"""Scoped MQTT transport, cache recovery and material observation notifications."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from homeassistant.components import mqtt
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import MAX_DEVICES, MAX_PACKET_BYTES
from .observation import DeviceObservation, FieldObservation, ObservationStore
from .policy import (
    BindingRegistry,
    PolicyError,
    SourceKind,
    StatePolicy,
    build_all_status_query,
    identifier,
)
from .protocol import (
    control_profile,
    interpret_output,
    native_control_message,
    parameter_getter_messages,
    parse_message,
)

_LOGGER = logging.getLogger(__name__)
FieldKey = tuple[str, int | None, str]
QUERY_PROFILE = "observed_legacy_lanbon_a_m2s_v1"


@dataclass
class Snapshot:
    """Bounded same-generation inventory: partial chunks never supply state."""

    token: Any
    total: int | None = None
    rows: dict[str, dict] = field(default_factory=dict)
    invalid: bool = False

    def add(self, event: dict) -> list[dict] | None:
        rows = event.get("devices", [])
        total = event.get("declared_total_count", len(rows))
        if (
            not isinstance(total, int)
            or not 0 <= total <= MAX_DEVICES
            or not isinstance(rows, list)
            or len(rows) > 44
            or len({row.get("device_id") for row in rows}) != len(rows)
            or (self.total is not None and self.total != total)
        ):
            self.invalid = True
            return None
        self.total = total
        for row in rows:
            peer = row["device_id"]
            if peer in self.rows and self.rows[peer] != row:
                self.invalid = True
                return None
            self.rows[peer] = row
        if len(self.rows) > total:
            self.invalid = True
        return list(self.rows.values()) if not self.invalid and len(self.rows) == total else None


class LanbonHub:
    """One broker entry owns all explicitly accepted groups.

    A string group retains the alpha observer constructor for compatibility.
    Runtime v2 passes a list and StatePolicy owns state/query/lease decisions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        group_id: str | list[str],
        prefix: str,
        max_age: int,
        *,
        enable_controls=False,
        allow_existing_controls=False,
        query_on_start=False,
        query_interval=0,
        entry_id=None,
    ):
        self.hass, self.prefix, self.max_age = hass, prefix, max_age
        self._now = hass.loop.time if hasattr(hass, "loop") else monotonic
        self.legacy = isinstance(group_id, str)
        self.groups = (group_id,) if self.legacy else tuple(dict.fromkeys(group_id))
        if not self.groups:
            raise ValueError("At least one accepted group is required")
        self.group_id, self.entry_id = self.groups[0], entry_id
        self.enable_controls = enable_controls and not self.legacy
        self.allow_existing_controls = allow_existing_controls is True and not self.legacy
        self.query_on_start, self.query_interval = query_on_start, query_interval
        self.store = ObservationStore(self.group_id, MAX_DEVICES, groups=self.groups)
        self.bindings = BindingRegistry()
        for group in self.groups:
            self.bindings.register_configured_group(group, trusted=True)
        self.policy = StatePolicy(
            self.bindings,
            max_report_age=max_age,
            root_cache_max_age=max(max_age, query_interval * 2),
            clock=self._now,
        )
        self._unsubscribers: list[Callable] = []
        self._listeners: set[Callable] = set()
        self._field_listeners: dict[FieldKey, set[Callable]] = {}
        self._diagnostic_listeners: set[Callable] = set()
        self._stopped = False
        self._ready: set[str] = set()
        self._topics: tuple[str, ...] = ()
        self._query_handle: asyncio.TimerHandle | None = None
        self._query_task: asyncio.Task | None = None
        self._next_query = 0.0 if query_on_start else self._now() + max(60, query_interval)
        self._last_diagnostic = 0.0
        self._snapshots: dict[str, Snapshot] = {}
        self.query_status: dict[str, str] = {}
        self.routes: dict[tuple[str, int | None], str] = {}
        self.conflicts: set[str] = set()
        self._signatures: dict[FieldKey, tuple | None] = {}
        self._route_store = (
            Store(hass, 1, f"lanbon_mqtt.{entry_id}.routes")
            if entry_id is not None and not self.legacy
            else None
        )
        self._route_records: dict[tuple, dict] = {}
        self._routes_loaded = False
        self._route_dirty = False
        self.group_diagnostics: dict[str, dict] = {}
        self.broker_diagnostics: dict[str, dict] = {}
        self._dirty_metadata_peers: set[str] = set()
        self._network_roots: dict[str, str] = {}
        self._parameter_task: asyncio.Task | None = None
        self._parameter_reads: dict[str, float] = {}
        self._parameter_windows: dict[tuple[str, str, str], tuple[int, float]] = {}
        self._native_metadata_routes: set[tuple[str, int]] = set()
        self._native_route_issues: dict[str, str] = {}
        self._registry_refresh_handle: asyncio.Handle | None = None

    def _parameter_guard(self, device, *, generation=None):
        peer, group = device.device_id, device.group_id
        base = self.store.devices.get((peer, None))
        qualified = base.metadata.live.get("device_type") if base is not None else None
        if (
            self.legacy
            or self._stopped
            or not self.store.connected
            or group not in self.groups
            or peer in self.conflicts
            or self.bindings.peer_owners.get(peer) != group
            or self.device_type(peer) not in {0x4A, 0x4B, 0x4C, 0x4D}
            or qualified is None
            or qualified.epoch != self.store.epoch
            or (generation is not None and generation != self.policy.generation)
        ):
            raise HomeAssistantError("Device parameters require a current qualified MQTT device")
        try:
            self.bindings.associate_peer(
                group, peer, now=self._now(), evidence_generation=self.bindings.evidence_generation
            )
        except PolicyError as error:
            raise HomeAssistantError(
                "Device parameters require current scoped membership"
            ) from error
        return peer, group

    def parameter_read_available(self, device):
        if (
            self._parameter_task is not None
            or self._now() - self._parameter_reads.get(device.device_id, float("-inf")) < 60
        ):
            return False
        try:
            self._parameter_guard(device)
        except HomeAssistantError:
            return False
        return True

    def _parameter_status_changed(self):
        self._emit({(peer, None, "parameter_read_status") for peer in self.bindings.peer_owners})

    async def async_read_parameters(self, device):
        """One explicit button press; never schedule or retry a parameter batch."""
        if not self.parameter_read_available(device):
            raise HomeAssistantError("A parameter read is active, cooling down or unavailable")
        peer, group = self._parameter_guard(device)
        if peer not in self._parameter_reads and len(self._parameter_reads) >= MAX_DEVICES:
            raise HomeAssistantError("Parameter read device limit reached")
        self._parameter_reads[peer] = self._now()
        generation = self.policy.generation
        task = self.hass.async_create_background_task(
            self._async_parameter_batch(device, group, peer, generation),
            "Lanbon manual parameter read",
            eager_start=False,
        )
        self._parameter_task = task
        self._parameter_status_changed()
        try:
            await task
        finally:
            if self._parameter_task is task:
                self._parameter_task = None
            if not self._stopped:
                self._parameter_status_changed()

    async def _async_parameter_batch(self, device, group, peer, generation):
        messages = parameter_getter_messages(group, peer, self.device_type(peer))
        for index, (topic, payload) in enumerate(messages):
            if self._parameter_guard(device, generation=generation) != (peer, group):
                raise HomeAssistantError("Device ownership changed during parameter read")
            mode = ("normal", "delay", "greenMode", "nightMode")[index]
            self._parameter_windows[(group, peer, mode)] = (generation, self._now() + 10)
            try:
                await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=False)
            except BaseException:
                self._parameter_windows.pop((group, peer, mode), None)
                raise
            if index < 3:
                await asyncio.sleep(0.25)

    def _cancel_parameter_read(self):
        if self._parameter_task is not None:
            self._parameter_task.cancel()
            self._parameter_task = None
        self._parameter_windows.clear()

    @callback
    def async_network_metadata_peers(self, group):
        """Current scoped typed targets only; no HTTP or ownership changes."""
        if group not in self.groups or self.legacy or self._stopped or not self.store.connected:
            return ()
        peers = []
        for (peer, channel), device in self.store.devices.items():
            if channel is not None or device.group_id != group:
                continue
            try:
                self._parameter_guard(device)
            except HomeAssistantError:
                continue
            peers.append(peer)
        return tuple(sorted(peers[:MAX_DEVICES]))

    def metadata_child_available(self, peer, group):
        return peer in self.async_network_metadata_peers(group)

    def _record_details(
        self, peer, group, family, data, values, *, received, source, retained=False, epoch=None
    ):
        if self.store.record_details(
            peer,
            group,
            family,
            data,
            values,
            received=received,
            source=source,
            confidence="source_format; live_compatibility_not_asserted",
            retained=retained,
            epoch=epoch,
        ):
            self._dirty_metadata_peers.add(peer)
            return True
        return False

    def _firmware_details(self, peer, group):
        device = self.store.devices.get((peer, None))
        if device is None or (observation := device.metadata.live.get("firmware_version")) is None:
            return
        previous = device.details.get("firmware")
        if (
            previous
            and previous.epoch == observation.epoch
            and previous.received >= observation.received
        ):
            return
        data = {"device_id": peer} | {
            key: item.value
            for key, item in device.metadata.live.items()
            if key.startswith("firmware_") or key == "raw_hex"
        }
        self._record_details(
            peer,
            group,
            "firmware",
            data,
            {
                "firmware_version": data.get("firmware_version"),
                "firmware_raw": data.get("firmware_raw"),
            },
            received=observation.received,
            epoch=observation.epoch,
            source="unsolicited_mqtt_report",
        )

    def _inventory_details(self, peer, group, row, received):
        profile = row.get("source_profile", {})
        values = {
            "device_type_raw": row["device_type"],
            "model": row.get("model"),
            "inventory_flags_raw": row.get("flags_raw_hex"),
            "inventory_flags_integer": row.get("flags_u16_le"),
            "inventory_measurement_1_raw": row.get("measurement_1_hex"),
            "inventory_measurement_2_raw": row.get("measurement_2_hex"),
            "inventory_auxiliary_raw": row.get("auxiliary_hex"),
            "inventory_record_raw": row.get("normalized_record_hex"),
            "temperature_decoder_candidate": profile.get("temperature_value"),
            "power_decoder_candidate": profile.get("power_value"),
        }
        for key, value in profile.get("feature_flags", {}).items():
            if key in {
                "normal_timer_running",
                "delay_active",
                "green_mode_enabled",
                "night_mode_running",
                "locked",
            }:
                values[f"root_{key}_candidate"] = value
        self._record_details(
            peer,
            group,
            "inventory",
            row,
            values,
            received=received,
            source="root_cache_query_response",
        )
        self._firmware_details(peer, group)

    def _report_details(self, event, received, *, retained=False, cache_source=None):
        group, kind = event.get("group_id"), event["kind"]
        source = cache_source or ("retained_mqtt_cache" if retained else "unsolicited_mqtt_report")
        if kind == "detail":
            if not retained:
                self.broker_diagnostics["detail"] = {
                    "count": event.get("count"),
                    "fields": event.get("fields"),
                    "raw_length": event.get("raw", {}).get("length"),
                    "raw_sha256": event.get("raw", {}).get("sha256"),
                }
            for row in event.get("devices", []):
                peer = row["device_id"]
                owner = self.bindings.peer_owners.get(peer)
                if owner in self.groups and peer in self.bindings.members.get(owner, set()):
                    self._record_details(
                        peer,
                        owner,
                        "firmware",
                        row,
                        {
                            "firmware_version": row.get("firmware_version"),
                            "firmware_raw": row.get("firmware_raw"),
                        },
                        received=received,
                        source=source,
                        retained=retained,
                    )
            return
        if group not in self.groups:
            return
        if kind == "energy_structure":
            if not retained:
                self.group_diagnostics.setdefault(group, {})["energy"] = {
                    key: value
                    for key, value in event.items()
                    if key not in {"devices", "raw", "topic"}
                }
            for row in event.get("devices", []):
                peer = row["device_id"]
                if self.bindings.peer_owners.get(peer) == group:
                    self._record_details(
                        peer,
                        group,
                        "energy",
                        row,
                        {
                            "energy_measurement_raw": row.get("measurement_hex"),
                            "energy_decoder_candidate": row.get("measurement_candidate"),
                        },
                        received=received,
                        source=source,
                        retained=retained,
                    )
                    self._firmware_details(peer, group)
            return
        if kind == "status_inventory":
            # Only caches take this path; current snapshots commit atomically in _inventory.
            if retained:
                for row in event.get("devices", []):
                    if self.bindings.peer_owners.get(row["device_id"]) == group:
                        self._record_details(
                            row["device_id"],
                            group,
                            "inventory",
                            row,
                            {},
                            received=received,
                            source=source,
                            retained=True,
                        )
            return
        if kind in {"unresolved_binary", "unresolved_direction"}:
            if not retained:
                self.group_diagnostics.setdefault(group, {})[kind] = {
                    "suffix": event.get("topic", "").split("/MAC:", 1)[-1][13:141],
                    "raw_length": event["raw"]["length"],
                    "raw_sha256": event["raw"]["sha256"],
                    "raw_prefix_hex": event["raw"]["hex"][:256],
                    "meaning": "uninterpreted; no peer attribution",
                }
            return
        peer = event.get("device_id")
        if peer is None or self.bindings.peer_owners.get(peer) != group:
            return
        if kind == "status":
            values = {
                "sync_auxiliary_raw": event["fields"].get("auxiliary_raw_hex"),
                "sync_marker_raw": event["fields"].get("marker"),
            }
            family = "sync"
        elif kind == "timer_structure":
            mode = event.get("timer_mode")
            if mode not in {"delay", "greenMode", "nightMode", "normal"}:
                return
            family = f"timer_{mode}"
            window = self._parameter_windows.get((group, peer, mode))
            if (
                not retained
                and window is not None
                and window[0] == self.policy.generation
                and self._now() <= window[1]
            ):
                source = "parameter_getter_response; peer_age_unknown"
            values = {
                f"timer_{mode}_duration": event["fields"].get("duration_seconds"),
                f"timer_{mode}_action_raw": event["fields"].get("action_raw"),
                f"timer_{mode}_schedule_raw": event["fields"].get("schedule_raw_hex"),
            }
            for slot in event["fields"].get("slots", []):
                if 1 <= slot["slot"] <= 8:
                    values[f"timer_slot_{slot['slot']}"] = slot
            if mode == "normal":
                values["timer_extension_raw"] = event["fields"].get("extension_raw_hex")
            if mode == "nightMode":
                fields = event["fields"]
                for boundary in ("start", "end"):
                    hour, minute = fields.get(f"{boundary}_hour"), fields.get(f"{boundary}_minute")
                    values[f"timer_night_{boundary}"] = (
                        f"{hour:02}:{minute:02}"
                        if fields.get("time_valid") and hour is not None and minute is not None
                        else None
                    )
                values["timer_night_all_day_candidate"] = fields.get("all_day_enabled_candidate")
                values["timer_night_scheduled_candidate"] = fields.get(
                    "scheduled_enabled_candidate"
                )
        elif kind in {"power", "brightness"}:
            family, values = kind, {}
        else:
            return
        data = {key: value for key, value in event.items() if key not in {"raw", "topic"}}
        data["raw_report"] = {
            "length": event["raw"]["length"],
            "sha256": event["raw"]["sha256"],
            "prefix_hex": event["raw"]["hex"][:256],
        }
        self._record_details(
            peer, group, family, data, values, received=received, source=source, retained=retained
        )
        self._firmware_details(peer, group)

    @callback
    def async_accept_network_metadata(self, metadata):
        """Accept a normalized verified-root envelope; never qualify a control."""
        if (
            self.legacy
            or self._stopped
            or not self.store.connected
            or not isinstance(metadata, dict)
        ):
            return False
        try:
            group, peer = (
                identifier(metadata["group_id"], physical=False),
                identifier(metadata["device_id"]),
            )
            if (
                metadata.get("source") != "read_only_http_metadata"
                or metadata.get("profile") != "mlink_get_device_info_v1"
            ):
                return False
            if (
                group not in self.groups
                or self.bindings.peer_owners.get(peer) != group
                or peer in self.conflicts
            ):
                return False
            role = metadata.get("role", "root")
            if role not in {"root", "child"}:
                return False
            if role == "child" and metadata.get("root_mac") != self._network_roots.get(group):
                return False
            self.bindings.associate_peer(
                group, peer, now=self._now(), evidence_generation=self.bindings.evidence_generation
            )
            fields = metadata["fields"]
            if not isinstance(fields, dict):
                return False
            before = self._discovery_signature()
            values = {
                f"network_{key}": value
                for key, value in fields.items()
                if key
                in {
                    "self_mac",
                    "mesh_id",
                    "parent_mac",
                    "ip_address",
                    "gateway_address",
                    "rssi",
                    "layer",
                    "version",
                    "idf_version",
                    "mlink_version",
                    "project_name",
                    "name",
                    "rssi_raw",
                    "rssi_fallback",
                    "mdf_version",
                    "mlink_trigger",
                    "tsf_time_raw",
                    "tid",
                    "mesh_node_count",
                    "status_code",
                    "status_msg",
                    "characteristics",
                }
            }
            if not self._record_details(
                peer,
                group,
                "network",
                metadata,
                values,
                received=dt_util.utcnow(),
                source="read_only_http_metadata",
            ):
                return False
            if role == "root":
                self._network_roots[group] = peer
            self._emit(set(), discover=before != self._discovery_signature())
            return True
        except ValueError, TypeError, KeyError, PolicyError:
            return False

    def _validated_route_record(self, record):
        if not isinstance(record, dict) or set(record) != {
            "group",
            "peer",
            "channel",
            "device_type",
            "native_base",
        }:
            raise ValueError("Invalid routing metadata")
        group = identifier(record["group"], physical=False)
        peer = identifier(record["peer"])
        kind, channel = record["device_type"], record["channel"]
        if (
            group not in self.groups
            or isinstance(kind, bool)
            or kind not in {0x4A, 0x4B, 0x4C}
            or isinstance(channel, bool)
            or not isinstance(channel, int)
            or not 1 <= channel <= kind - 0x49
        ):
            raise ValueError("Routing metadata no longer matches accepted scope")
        native_control_message(
            group,
            peer,
            kind,
            "power",
            value="ON",
            channel=channel,
            discovery_prefix=self.prefix,
            observed_native_base=record["native_base"],
        )
        return {
            "group": group,
            "peer": peer,
            "channel": channel,
            "device_type": kind,
            "native_base": record["native_base"],
        }

    async def async_load_routes(self):
        """Load routing candidates only; fresh inventory must requalify them."""
        if self._routes_loaded:
            return
        self._routes_loaded = True
        if self._route_store is None:
            return
        data = await self._route_store.async_load()
        if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
            return
        if len(data["routes"]) > MAX_DEVICES * 4:
            return
        peers = set()
        for raw in data["routes"]:
            try:
                record = self._validated_route_record(raw)
            except ValueError, TypeError, KeyError:
                continue
            peer_key = (record["group"], record["peer"])
            if peer_key not in peers and len(peers) >= MAX_DEVICES:
                continue
            peers.add(peer_key)
            key = (*peer_key, record["channel"], record["device_type"])
            self._route_records[key] = record

    def _route_payload(self):
        self._route_dirty = False
        return {"routes": [dict(value) for _, value in sorted(self._route_records.items())]}

    async def async_flush_routes(self):
        if self._route_store is not None and self._route_dirty:
            await self._route_store.async_save(self._route_payload())

    def _remember_routes(self, group, peer, kind):
        if kind not in {0x4A, 0x4B, 0x4C} or self._route_store is None:
            return
        peers = {(key[0], key[1]) for key in self._route_records}
        if (group, peer) not in peers and len(peers) >= MAX_DEVICES:
            return
        changed = False
        for channel in range(1, kind - 0x49 + 1):
            if (peer, channel) in self._native_metadata_routes:
                continue
            route = self.route_for(peer, channel)
            if route is None:
                continue
            try:
                record = self._validated_route_record(
                    {
                        "group": group,
                        "peer": peer,
                        "channel": channel,
                        "device_type": kind,
                        "native_base": route,
                    }
                )
            except ValueError, TypeError:
                continue
            key = (group, peer, channel, kind)
            if key not in self._route_records and len(self._route_records) >= MAX_DEVICES * 4:
                continue
            if self._route_records.get(key) != record:
                self._route_records[key] = record
                changed = True
        if changed:
            self._route_dirty = True
            # Schedule only a new or changed validated route, never each report.
            self._route_store.async_delay_save(self._route_payload, 5)

    def _requalify_routes(self, group, peer, kind):
        for record in self._route_records.values():
            if (record["group"], record["peer"], record["device_type"]) == (
                group,
                peer,
                kind,
            ) and self.route_for(peer, record["channel"]) is None:
                self.routes.setdefault((peer, record["channel"]), record["native_base"])
        self._remember_routes(group, peer, kind)

    def _learn_native_routes(self):
        """Use active HA MQTT subscription addresses as routing metadata only."""
        if self.legacy or not self.allow_existing_controls or not self.enable_controls:
            return False
        targets = {}
        for peer, group in self.bindings.peer_owners.items():
            kind = self.device_type(peer)
            base = self.store.devices.get((peer, None))
            if kind not in {0x4A, 0x4B, 0x4C} or base is None:
                continue
            try:
                self._parameter_guard(base)
            except HomeAssistantError:
                continue
            targets[peer] = (group, kind)
        debug_entities = getattr(self.hass.data.get("mqtt"), "debug_info_entities", {})
        if not isinstance(debug_entities, dict):
            debug_entities = {}
        candidates = {}
        for entity in er.async_get(self.hass).entities.values():
            if (
                entity.platform != "mqtt"
                or not entity.entity_id.startswith(("light.", "switch."))
                or entity.entity_category in {EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC}
                or entity.disabled_by is not None
            ):
                continue
            debug = debug_entities.get(entity.entity_id)
            if not isinstance(debug, dict) or not isinstance(debug.get("subscriptions"), dict):
                continue
            discovery = debug.get("discovery_data", {})
            config = discovery.get("discovery_payload", {}) if isinstance(discovery, dict) else {}
            if not isinstance(config, dict):
                continue
            for topic, subscription in debug["subscriptions"].items():
                if (
                    not isinstance(topic, str)
                    or len(topic) > 256
                    or not topic.startswith(self.prefix + "/")
                    or any(character in topic for character in "+#")
                    or not isinstance(subscription, dict)
                    or type(subscription.get("count")) is not int
                    or subscription["count"] < 1
                ):
                    continue
                parts = topic[len(self.prefix) + 1 :].split("/")
                if len(parts) != 4 or parts[-1] != "state":
                    continue
                group, component, address, _ = parts
                peer = address[:12].upper()
                if peer not in targets or group.upper() != targets[peer][0]:
                    continue
                base = topic.rsplit("/", 1)[0]
                if any(
                    key in config and config[key] != base + "/set"
                    for key in ("command_topic", "cmd_t")
                ):
                    continue
                group, kind = targets[peer]
                for channel in range(1, kind - 0x49 + 1):
                    try:
                        native_control_message(
                            group,
                            peer,
                            kind,
                            "power",
                            value="ON",
                            channel=channel,
                            discovery_prefix=self.prefix,
                            observed_native_base=base,
                            component=component,
                        )
                    except ValueError:
                        continue
                    key = (peer, channel)
                    if key not in candidates and len(candidates) >= MAX_DEVICES * 4:
                        continue
                    # Two distinct routes suffice to prove ambiguity; do not keep a history.
                    values = candidates.setdefault(key, set())
                    if len(values) < 2:
                        values.add(base)
        changed = False
        self._native_route_issues.clear()
        for key in tuple(self._native_metadata_routes):
            if len(candidates.get(key, ())) != 1:
                self.routes.pop(key, None)
                self._native_metadata_routes.discard(key)
                changed = True
        for (peer, channel), values in candidates.items():
            if len(values) != 1:
                self._native_route_issues[peer] = "ambiguous_native_routes"
                continue
            route = next(iter(values))
            if (peer, channel) in self._native_metadata_routes:
                if self.routes.get((peer, channel)) != route:
                    self.routes[(peer, channel)] = route
                    changed = True
                continue
            if self.route_for(peer, channel) is not None:
                continue
            self.routes[(peer, channel)] = route
            self._native_metadata_routes.add((peer, channel))
            changed = True
        return changed

    async def async_start(self):
        await self.async_load_routes()
        self._stopped = False
        self.store.set_connected(mqtt.is_connected(self.hass))
        self.policy.active = self.store.connected
        topics = ["lanbon/a/L8devCompleteDetailReport"]
        if not self.legacy:
            topics.append("lanbon/a/L8devElecsumReport")
        for group in self.groups:
            topics.append(f"lanbon/a/MAC:{group}/#")
            if not self.legacy:
                topics.append(f"lanbon/b/MAC:{group}/#")
            topics.append(f"{self.prefix}/{group}/#")
        self._topics = tuple(topics)
        try:
            self._unsubscribers.append(
                mqtt.async_subscribe_connection_status(self.hass, self._connection_changed)
            )
            for topic in self._topics:
                if self.query_on_start or self.query_interval:
                    # Register before the local subscription; processed-topic
                    # events are emitted after HA waits for the corresponding SUBACK.
                    self._unsubscribers.append(
                        mqtt.async_on_subscribe_done(
                            self.hass, topic, 0, lambda topic=topic: self._subscription_ready(topic)
                        )
                    )
                self._unsubscribers.append(
                    await mqtt.async_subscribe(
                        self.hass, topic, self._message_received, qos=0, encoding=None
                    )
                )
            self._unsubscribers.append(
                async_track_time_interval(self.hass, self._tick, timedelta(seconds=30))
            )
            if not self.legacy:
                self._unsubscribers.append(
                    self.hass.bus.async_listen("entity_registry_updated", self._registry_changed)
                )
                self._emit(set(), diagnostic=True)
        except BaseException:
            self.async_stop()
            raise

    @callback
    def async_stop(self):
        self._stopped = True
        if self._registry_refresh_handle:
            self._registry_refresh_handle.cancel()
            self._registry_refresh_handle = None
        self._cancel_parameter_read()
        if self._query_handle:
            self._query_handle.cancel()
            self._query_handle = None
        if self._query_task:
            self._query_task.cancel()
            self._query_task = None
        self._ready.clear()
        self._network_roots.clear()
        self._snapshots.clear()
        self.policy.reset_capture(active=False)
        self.store.set_connected(False)
        while self._unsubscribers:
            self._unsubscribers.pop()()
        self._notify()
        self._listeners.clear()
        self._field_listeners.clear()
        self._diagnostic_listeners.clear()

    @callback
    def async_add_listener(self, listener, key: FieldKey | str | None = None):
        if key == "diagnostic":
            listeners = self._diagnostic_listeners
        elif key is None:
            listeners = self._listeners
        else:
            listeners = self._field_listeners.setdefault(key, set())
        listeners.add(listener)

        @callback
        def remove():
            listeners.discard(listener)

        return remove

    @callback
    def _notify(self):
        listeners = set(self._listeners) | self._diagnostic_listeners
        for values in self._field_listeners.values():
            listeners.update(values)
        for listener in tuple(listeners):
            listener()

    def _emit(self, keys, *, discover=False, diagnostic=False):
        listeners = set(self._listeners) if discover else set()
        for key in keys:
            listeners.update(self._field_listeners.get(key, ()))
        if diagnostic:
            listeners.update(self._diagnostic_listeners)
        for listener in tuple(listeners):
            listener()

    @callback
    def _connection_changed(self, connected):
        if self._stopped or self.store.connected == connected:
            return
        self.store.set_connected(connected)
        self._cancel_parameter_read()
        self.policy.reset_capture(active=connected)
        self._snapshots.clear()
        self._ready.clear()
        self._network_roots.clear()
        if self._query_handle:
            self._query_handle.cancel()
            self._query_handle = None
        if self._query_task:
            self._query_task.cancel()
            self._query_task = None
        self._next_query = 0 if self.query_on_start else self._now() + max(60, self.query_interval)
        self._notify()

    @callback
    def _subscription_ready(self, topic):
        if self._stopped or not self.store.connected:
            return
        self._ready.add(topic)
        if (
            self.query_on_start
            and set(self._topics) <= self._ready
            and self._now() >= self._next_query
        ):
            self._schedule_query()

    def _schedule_query(self):
        if not self._query_handle and not self._query_task and not self._stopped:
            self._query_handle = self.hass.loop.call_later(5, self._launch_query)

    @callback
    def _launch_query(self):
        self._query_handle = None
        if not self._stopped:
            self._query_task = self.hass.async_create_background_task(
                self.async_query_groups(), "Lanbon cache recovery queries"
            )

            def finished(task):
                if self._query_task is task:
                    self._query_task = None

            self._query_task.add_done_callback(finished)

    async def async_query_groups(self):
        if self._stopped or not self.store.connected or not set(self._topics) <= self._ready:
            return
        next_due = []
        for group in self.groups:
            now = self._now()
            due = now + max(60, self.query_interval or 300)
            try:
                token = self.policy.begin_query(
                    group, now=now, subscriptions_ready=True, timeout=10, minimum_interval=60
                )
                intent = build_all_status_query(group, route_profile=QUERY_PROFILE)
                self._snapshots[group] = Snapshot(token)
                self.query_status[group] = "awaiting_root_cache"
                await mqtt.async_publish(
                    self.hass, intent.topic, intent.payload, qos=intent.qos, retain=False
                )
            except PolicyError as error:
                self.query_status[group] = str(error)
                if str(error) == "query_rate_limited":
                    due = self.policy.last_query.get(group, now) + 60
            except HomeAssistantError:
                self.query_status[group] = "query_publish_failed"
                self._snapshots.pop(group, None)
                _LOGGER.warning("Lanbon cache query could not be published")
            next_due.append(due)
        self._next_query = min(next_due)

    @callback
    def _tick(self, now: datetime):
        if self._stopped:
            return
        if self.legacy:
            self._notify()
            return
        monotonic = self._now()
        keys = self._refresh_fields()
        keys.update((peer, None, "parameter_read_status") for peer in self.bindings.peer_owners)
        self._parameter_windows = {
            key: value
            for key, value in self._parameter_windows.items()
            if value[0] == self.policy.generation and monotonic <= value[1]
        }
        for group, snapshot in tuple(self._snapshots.items()):
            if monotonic > snapshot.token.expires:
                self.query_status[group] = "root_cache_timeout"
                del self._snapshots[group]
        diagnostic = monotonic - self._last_diagnostic >= 60
        if diagnostic:
            self._last_diagnostic = monotonic
            for peer, channel in self.store.devices:
                if peer in self._dirty_metadata_peers:
                    keys.update(
                        (peer, channel, field) for field in ("power", "brightness", "output_code")
                    )
            self._dirty_metadata_peers.clear()
            self._update_device_metadata()
        routes_changed = self._learn_native_routes() if diagnostic else False
        self._emit(keys, diagnostic=diagnostic, discover=routes_changed)
        if (
            self.query_interval
            and monotonic >= self._next_query
            and self.store.connected
            and set(self._topics) <= self._ready
        ):
            self._schedule_query()

    def _register_peer(self, group, peer, kind, now):
        if peer not in self.bindings.peer_owners and len(self.bindings.peer_owners) >= MAX_DEVICES:
            raise PolicyError("physical_peer_limit")
        self.bindings.record_mqtt_member(
            group,
            peer,
            retained=False,
            report_kind=kind,
            now=now,
            evidence_generation=self.bindings.evidence_generation,
            broker_scope=self.bindings.broker_scope,
        )
        self.bindings.associate_peer(
            group, peer, now=now, evidence_generation=self.bindings.evidence_generation
        )

    @staticmethod
    def _policy_field(metric, channel):
        return metric if channel is None else f"{metric}_{channel}"

    def observation(self, device, metric):
        if not device.group_id:
            return None
        try:
            return self.policy.current(
                device.group_id,
                device.device_id,
                self._policy_field(metric, device.channel),
                now=self._now(),
            )
        except PolicyError:
            return None

    def _refresh_fields(self):
        keys = set()
        for (peer, channel), device in tuple(self.store.devices.items()):
            for metric in ("power", "brightness", "output_code"):
                observation = self.observation(device, metric)
                signature = (
                    None
                    if observation is None
                    else (observation.value, observation.source, observation.generation)
                )
                key = (peer, channel, metric)
                if self._signatures.get(key) != signature:
                    keys.add(key)
                    self._signatures[key] = signature
                if observation is not None:
                    received = dt_util.utcnow() - timedelta(
                        seconds=max(0, self._now() - observation.received_monotonic)
                    )
                    device.fields[metric] = FieldObservation(
                        observation.value,
                        received,
                        self.store.epoch,
                        "",
                        "reported_state; physical_state_unverified",
                        False,
                        observation.source.value,
                    )
                    if observation.source is not SourceKind.ROOT_CACHE:
                        device.last_received, device.last_epoch = received, self.store.epoch
        return keys

    def _apply_value(self, group, peer, channel, metric, value, now, *, token=None):
        name = self._policy_field(metric, channel)
        if token is None:
            self.policy.accept_mqtt(
                group, peer, name, value, retained=False, now=now, generation=self.policy.generation
            )
        else:
            self.policy.accept_root_cache(token, peer, name, value, retained=False, now=now)
        if device := self.store._device(peer, channel):
            device.group_id = group

    def _inventory(self, event, message, now, received):
        group = event["group_id"]
        snapshot = self._snapshots.get(group)
        if message.retain is not False or snapshot is None:
            return
        token = snapshot.token
        if token.generation != self.policy.generation or not token.started <= now <= token.expires:
            return
        rows = snapshot.add(event)
        if snapshot.invalid:
            self.query_status[group] = "invalid_or_conflicting_inventory"
            return
        if rows is None:
            self.query_status[group] = "partial_root_cache"
            return
        self._snapshots.pop(group, None)
        self.query_status[group] = "complete_root_cache; peer_age_unknown"
        accepted = []
        for row in rows:
            peer = row["device_id"]
            previous_type = self.device_type(peer)
            if previous_type is not None and previous_type != row["device_type"]:
                self.conflicts.add(peer)
                continue
            try:
                self._register_peer(group, peer, "status_inventory", now)
            except PolicyError:
                self.conflicts.add(peer)
                continue
            accepted.append(row)
            self._apply_value(group, peer, None, "output_code", row["status_raw"], now, token=token)
            profile = control_profile(row["device_type"])
            if not profile:
                continue
            raw = row["status_raw"]
            if row["device_type"] == 0x4D:
                # The first inventory can qualify a sync report that arrived
                # while this query was in flight. Its later raw revision must
                # also win for the newly interpreted power/brightness fields.
                raw_key = (group, peer, "output_code")
                direct = self.policy.fields.get(raw_key)
                if (
                    direct is not None
                    and direct.generation == self.policy.generation
                    and direct.source is SourceKind.DIRECT_MQTT
                    and self.policy.direct_revisions.get(raw_key, 0) > token.start_revision
                ):
                    self._dimmer_values(
                        group, peer, direct.value, direct.received_monotonic, context="sync"
                    )
                self._dimmer_values(group, peer, raw, now, context="inventory", token=token)
            elif row["device_type"] in {0x4A, 0x4B, 0x4C}:
                if row["device_type"] == 0x4A:
                    native_key = (group, peer, "power")
                    direct = self.policy.fields.get(native_key)
                    revision = self.policy.direct_revisions.get(native_key, 0)
                    if (
                        direct is not None
                        and direct.generation == self.policy.generation
                        and direct.source is SourceKind.DIRECT_MQTT
                        and revision > token.start_revision
                        and revision > self.policy.direct_revisions.get((group, peer, "power_1"), 0)
                    ):
                        # A suffixless native single-relay report first seen
                        # during bootstrap becomes channel1 only after type
                        # qualification; preserve its actual receipt/precedence.
                        self._apply_value(
                            group, peer, 1, "power", direct.value, direct.received_monotonic
                        )
                for channel in range(1, row["device_type"] - 0x49 + 1):
                    self._apply_value(
                        group,
                        peer,
                        channel,
                        "power",
                        "ON" if raw & (1 << (channel - 1)) else "OFF",
                        now,
                        token=token,
                    )
        self.store.accept(
            dict(event, devices=accepted), retained=False, received=received, update_fields=False
        )
        for row in accepted:
            self._requalify_routes(group, row["device_id"], row["device_type"])
            self._inventory_details(row["device_id"], group, row, received)
        self._learn_native_routes()
        self.group_diagnostics.setdefault(group, {})["inventory"] = {
            key: value for key, value in event.items() if key not in {"devices", "raw", "topic"}
        }
        self.group_diagnostics[group]["inventory"].update(
            accepted_count=len(accepted),
            complete_count=snapshot.total,
            connection_generation=self.store.epoch,
        )

    def _dimmer_values(self, group, peer, raw, now, *, context, token=None):
        values = interpret_output(0x4D, raw, context=context, qualified=True)
        if (percent := values.get("output_percent")) is None:
            return
        self._apply_value(group, peer, None, "power", values["reported_power"], now, token=token)
        if percent:
            self._apply_value(group, peer, None, "brightness", percent, now, token=token)

    @callback
    def _message_received(self, message: ReceiveMessage):
        if self._stopped or not isinstance(message.payload, bytes):
            return
        if len(message.payload) > MAX_PACKET_BYTES:
            self.store.rejected_messages += 1
            return
        event = parse_message(message.topic, message.payload)
        if event is None:
            return
        received = dt_util.utcnow()
        if self.legacy:
            self.store.accept(event, retained=message.retain, received=received)
            self._notify()
            return
        if not self.store.connected:
            return
        group, kind = event.get("group_id"), event["kind"]
        if group is not None and group not in self.groups:
            return
        if event.get("discovery_prefix", self.prefix) != self.prefix:
            return
        self.store.transport_messages += 1
        count_key = (
            kind
            if kind
            in {
                "power",
                "brightness",
                "status",
                "status_inventory",
                "detail",
                "energy_structure",
                "timer_structure",
                "command",
                "query",
            }
            else "unresolved"
        )
        self.store.family_counts[count_key] = self.store.family_counts.get(count_key, 0) + 1
        if message.retain is not False:
            # Broker caches must not allocate live-device capacity or mutate a
            # trusted peer's group. The v1 observer retains its separate audit API.
            self.store.messages += 1
            self.store.retained_messages += 1
            try:
                self._report_details(
                    event,
                    received,
                    retained=True,
                    cache_source="unknown_retain_cache"
                    if message.retain is None
                    else "retained_mqtt_cache",
                )
            except ValueError, TypeError, KeyError:
                self.store.rejected_messages += 1
            return
        now = self._now()
        before = self._discovery_signature()
        try:
            if kind == "status_inventory" and group in self._network_roots:
                event = parse_message(
                    message.topic, message.payload, root_mac=self._network_roots[group]
                )
            if kind == "status_inventory":
                self._inventory(event, message, now, received)
            elif kind in {"power", "brightness", "status"}:
                peer = event["device_id"]
                self._register_peer(group, peer, kind, now)
                self.store.accept(event, retained=False, received=received, update_fields=False)
                channel = event.get("channel")
                if kind in {"power", "brightness"}:
                    self._native_metadata_routes.discard((peer, channel))
                    if self.device_type(peer) == 0x4A and channel is None:
                        self._native_metadata_routes.discard((peer, 1))
                    self._native_route_issues.pop(peer, None)
                    if self.device_type(peer) == 0x4A and channel in {None, 1}:
                        self.routes.pop((peer, 1 if channel is None else None), None)
                    self.routes[(peer, channel)] = message.topic.rsplit("/", 1)[0]
                    if self.device_type(peer) == 0x4A and channel is None:
                        channel = 1
                    self._apply_value(group, peer, channel, kind, event["fields"][kind], now)
                    self._remember_routes(group, peer, self.device_type(peer))
                else:
                    raw = event["fields"]["output_code"]
                    self._apply_value(group, peer, None, "output_code", raw, now)
                    if self.device_type(peer) == 0x4D:
                        self._dimmer_values(group, peer, raw, now, context="sync")
            elif kind == "energy_structure" and message.retain is False:
                for row in event.get("devices", []):
                    self._register_peer(group, row["device_id"], "status_inventory", now)
            elif kind == "detail":
                self.store.accept(event, retained=message.retain is not False, received=received)
            self._report_details(event, received)
        except PolicyError, KeyError, ValueError:
            self.store.rejected_messages += 1
            if peer := event.get("device_id"):
                self.conflicts.add(peer)
            return
        self._emit(self._refresh_fields(), discover=before != self._discovery_signature())

    def _discovery_signature(self):
        return (
            tuple(
                (
                    key,
                    device.group_id,
                    device.detail.get("device_type"),
                    device.detail.get("model_confidence"),
                    tuple(device.diagnostics),
                    tuple(device.details),
                )
                for key, device in self.store.devices.items()
            ),
            tuple(sorted(self.routes.items(), key=str)),
            tuple(sorted(self.conflicts)),
        )

    def device_type(self, peer):
        for (device_id, _), device in self.store.devices.items():
            if (
                device_id == peer
                and device.detail.get("model_confidence") == "source_verified_type_enum"
            ):
                return device.detail.get("device_type")
        return None

    def _update_device_metadata(self):
        """Update only our stable physical device record, without connections/merges."""
        registry = dr.async_get(self.hass)
        for (peer, channel), observation in tuple(self.store.devices.items()):
            if channel is not None:
                continue
            device = registry.async_get_device_by_identifier(("lanbon_mqtt", peer), self.entry_id)
            if device is None:
                continue
            changes = {}
            firmware = observation.diagnostics.get("firmware_version")
            if firmware and device.sw_version != firmware.value:
                changes["sw_version"] = str(firmware.value)
            model = observation.diagnostics.get("model")
            if model and model.value != "Unresolved type" and device.model != model.value:
                changes["model"] = str(model.value)
            if changes:
                registry.async_update_device(device.id, **changes)

    def route_for(self, peer, channel):
        kind = self.device_type(peer)
        if self.allow_existing_controls and kind in {0x4A, 0x4B, 0x4C}:
            base = self.store.devices.get((peer, None))
            try:
                if base is None:
                    return None
                self._parameter_guard(base)
            except HomeAssistantError:
                return None
        route = self.routes.get((peer, channel))
        if route is None and kind == 0x4A and channel == 1:
            route = self.routes.get((peer, None))
        if route is not None:
            try:
                native_control_message(
                    self.bindings.peer_owners[peer],
                    peer,
                    kind,
                    "power",
                    value="ON",
                    channel=channel,
                    discovery_prefix=self.prefix,
                    observed_native_base=route,
                )
            except ValueError, TypeError, KeyError:
                return None
        return route

    def existing_control_entities(self, peer):
        """Find functional owners; network administration is not lamp control."""
        registry, devices = er.async_get(self.hass), dr.async_get(self.hass)
        matches = []
        for entity in registry.entities.values():
            if (
                entity.platform == "lanbon_mqtt"
                or not entity.entity_id.startswith(("light.", "switch."))
                or entity.entity_category in {EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC}
            ):
                continue
            candidates = [entity.unique_id, entity.entity_id]
            if entity.device_id and (device := devices.async_get(entity.device_id)):
                candidates += [value for _, value in device.identifiers | device.connections]
            data = self.hass.data.get("mqtt")
            debug_entities = getattr(data, "debug_info_entities", {})
            if isinstance(debug_entities, dict) and (debug := debug_entities.get(entity.entity_id)):
                candidates += list(debug.get("subscriptions", {}))
                discovery = debug.get("discovery_data", {}).get("discovery_payload", {})
                if isinstance(discovery, dict):
                    candidates += [
                        discovery.get(key, "") for key in ("state_topic", "command_topic")
                    ]
            if any(
                peer in str(value).upper().replace(":", "").replace("-", "") for value in candidates
            ):
                matches.append(entity.entity_id)
                if len(matches) == 16:
                    break
        return tuple(sorted(matches))

    def suppressed(self, peer):
        return not self.allow_existing_controls and bool(self.existing_control_entities(peer))

    def control_status(self, peer):
        """Explain configuration and route readiness, never physical delivery."""
        existing = self.existing_control_entities(peer)
        profile = control_profile(self.device_type(peer))
        base = self.store.devices.get((peer, None))
        raw_type = base.metadata.live.get("device_type") if base is not None else None
        channels = []
        status, explanation = (
            "ready",
            "Lanbon controls are enabled. Reports determine displayed state.",
        )
        if not self.enable_controls:
            status, explanation = (
                "disabled",
                "Turn on Enable supported light and switch controls in integration options.",
            )
        elif peer in self.conflicts:
            status, explanation = (
                "membership_conflict",
                "Resolve conflicting mesh ownership before controlling this device.",
            )
        elif not self.store.connected:
            status, explanation = (
                "mqtt_disconnected",
                "Restore the native Home Assistant MQTT connection.",
            )
        elif profile is None:
            if raw_type is not None and raw_type.value not in {0x4A, 0x4B, 0x4C, 0x4D}:
                status, explanation = (
                    "unsupported_type",
                    "This inventoried output type has no qualified control profile.",
                )
            else:
                status, explanation = (
                    "waiting_for_inventory",
                    "A supported device type must be qualified by a complete root-cache inventory.",
                )
        elif existing and not self.allow_existing_controls:
            status, explanation = (
                "existing_controls_preserved",
                "Use existing entities, or enable Add Lanbon controls alongside existing controls "
                "in integration options.",
            )
        elif profile["requires_observed_native_route"]:
            channels = [
                channel
                for channel in range(1, profile["channels"] + 1)
                if self.route_for(peer, channel)
            ]
            if len(channels) != profile["channels"]:
                status = "partial_routes" if channels else "waiting_for_route"
                explanation = (
                    "Some relay channels need a validated native MQTT route. "
                    "Enable the existing native MQTT entity, or await its native feedback."
                )
                if self._native_route_issues.get(peer) == "ambiguous_native_routes":
                    explanation = (
                        "Conflicting native MQTT routes exist for a relay channel; "
                        "no route was selected for that channel."
                    )
        return {
            "status": status,
            "explanation": explanation,
            "existing_control_entities": existing,
            "existing_controls_list_limit": 16,
            "allow_existing_controls": self.allow_existing_controls,
            "controls_enabled": self.enable_controls,
            "qualified_relay_channels": channels,
            "physical_state_verified": False,
            "peer_observation_age_known": False,
        }

    @callback
    def _registry_changed(self, event):
        entity_id = event.data.get("entity_id", "")
        if self._stopped or not entity_id.startswith(("light.", "switch.")):
            return
        entity = er.async_get(self.hass).async_get(entity_id)
        if entity is not None and entity.platform == "lanbon_mqtt":
            return
        if self._registry_refresh_handle is None:
            self._registry_refresh_handle = self.hass.loop.call_soon(self._refresh_control_registry)

    @callback
    def _refresh_control_registry(self):
        self._registry_refresh_handle = None
        if not self._stopped:
            self._learn_native_routes()
            self._notify()

    async def async_command(self, device: DeviceObservation, action, value=None):
        peer, channel = device.device_id, device.channel
        if (peer, channel) in self._native_metadata_routes and self._learn_native_routes():
            self._notify()
        if (
            not self.enable_controls
            or not self.store.connected
            or peer in self.conflicts
            or self.suppressed(peer)
            or not device.group_id
        ):
            raise HomeAssistantError("Lanbon control is unavailable or owned by another entity")
        try:
            topic, payload = native_control_message(
                device.group_id,
                peer,
                self.device_type(peer),
                action,
                value=value,
                channel=channel,
                discovery_prefix=self.prefix,
                observed_native_base=self.route_for(peer, channel),
            )
        except ValueError as error:
            raise HomeAssistantError(
                "Lanbon command has no qualified native route or value"
            ) from error
        await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=False)
        # Intent only: successful publication never writes or confirms state.
