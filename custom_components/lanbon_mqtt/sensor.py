"""Diagnostic reported values; these sensors do not represent verified actuators."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import LanbonConfigEntry
from .const import DOMAIN, NAME
from .hub import LanbonHub
from .observation import DeviceObservation, FieldObservation

DESCRIPTIONS = (
    SensorEntityDescription(key="power", name="Reported power"),
    SensorEntityDescription(
        key="brightness", name="Reported brightness", native_unit_of_measurement=PERCENTAGE
    ),
    SensorEntityDescription(key="output_code", name="Native output code"),
    SensorEntityDescription(
        key="last_received",
        name="Last live report",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
)

# Unitless fields deliberately retain raw/profile labels. No energy, power or
# temperature device class/state_class is assigned to unqualified triplets.
DETAIL_NAMES = {
    "firmware_version": "Reported firmware",
    "firmware_raw": "Firmware integer",
    "model": "Reported type family",
    "device_type_raw": "Device type code",
    "inventory_flags_raw": "Inventory flags raw",
    "inventory_flags_integer": "Inventory flags integer",
    "inventory_measurement_1_raw": "Inventory measurement 1 raw",
    "inventory_measurement_2_raw": "Inventory measurement 2 raw",
    "inventory_auxiliary_raw": "Inventory auxiliary raw",
    "inventory_record_raw": "Inventory record raw",
    "temperature_decoder_candidate": "Temperature decoder candidate (unit unverified)",
    "power_decoder_candidate": "Power decoder candidate (unit unverified)",
    "energy_measurement_raw": "Energy report triplet raw",
    "energy_decoder_candidate": "Energy decoder candidate (unit unverified)",
    "sync_auxiliary_raw": "Sync auxiliary raw",
    "sync_marker_raw": "Sync marker raw",
    "root_normal_timer_running_candidate": "Root normal timer running flag candidate",
    "root_delay_active_candidate": "Root delay active flag candidate",
    "root_green_mode_enabled_candidate": "Root green mode flag candidate",
    "root_night_mode_running_candidate": "Root night mode running flag candidate",
    "root_locked_candidate": "Root lock flag candidate",
    "timer_delay_duration": "Delay remaining seconds at receipt",
    "timer_greenMode_duration": "Configured inactivity period",
    "timer_delay_action_raw": "Delay timer action raw",
    "timer_nightMode_schedule_raw": "Night timer schedule raw",
    "timer_extension_raw": "Timer extension raw",
    "timer_night_start": "Night mode configured start",
    "timer_night_end": "Night mode configured end",
    "timer_night_all_day_candidate": "Night mode all day configuration candidate",
    "timer_night_scheduled_candidate": "Night mode scheduled configuration candidate",
    **{f"timer_slot_{slot}": f"Timer slot {slot} reported schedule" for slot in range(1, 9)},
    "network_self_mac": "Mesh node self MAC",
    "network_mesh_id": "Mesh topic ID",
    "network_parent_mac": "Mesh node parent MAC",
    "network_ip_address": "Root network address",
    "network_gateway_address": "Metadata gateway address",
    "network_rssi": "Parent link RSSI",
    "network_layer": "Mesh layer",
    "network_version": "Mlink framework application version",
    "network_idf_version": "Node ESP IDF version",
    "network_mlink_version": "Node Mlink framework version",
    "network_project_name": "Node framework project",
    "network_name": "Node framework name",
    "network_rssi_raw": "Parent link RSSI raw",
    "network_rssi_fallback": "SDK RSSI fallback flag",
    "network_mdf_version": "Node ESP MDF version",
    "network_mlink_trigger": "Node Mlink trigger raw",
    "network_tsf_time_raw": "Node TSF raw",
    "network_tid": "Node framework transaction raw",
    "network_mesh_node_count": "Root reported mesh node count",
    "network_status_code": "Root metadata status code",
    "network_status_msg": "Root metadata status message",
    "network_characteristics": "Node framework characteristics raw",
}
STATIC_DETAILS = {"firmware_version", "firmware_raw", "model", "device_type_raw"}
ENABLED_DETAILS = {"firmware_version", "model", "device_type_raw"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LanbonConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover channel sensors from group-attributed reports."""
    hub = entry.runtime_data
    added: set[tuple[str, int | None]] = set()
    detail_added: set[tuple[str, str]] = set()
    group_added: set[str] = set()
    async_add_entities([LanbonCaptureSensor(hub)])

    @callback
    def discover() -> None:
        entities: list[SensorEntity] = []
        for key, device in tuple(hub.store.devices.items()):
            if key not in added:
                added.add(key)
                entities.extend(LanbonObservationSensor(hub, device, d) for d in DESCRIPTIONS)
            if not hub.legacy and device.channel is None:
                identity = (device.device_id, "control_status")
                if identity not in detail_added:
                    detail_added.add(identity)
                    entities.append(LanbonControlStatusSensor(hub, device))
                for detail in ("device_details", "diagnostic_report_received", *device.diagnostics):
                    identity = (device.device_id, detail)
                    if identity not in detail_added and (
                        detail in DETAIL_NAMES
                        or detail in {"device_details", "diagnostic_report_received"}
                    ):
                        detail_added.add(identity)
                        entities.append(LanbonDetailSensor(hub, device, detail))
        if not hub.legacy:
            for group in hub.group_diagnostics:
                if group not in group_added:
                    group_added.add(group)
                    entities.append(LanbonGroupDetailsSensor(hub, group))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(hub.async_add_listener(discover))
    discover()


class LanbonBaseSensor(SensorEntity):
    """Common MQTT observation lifecycle."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hub: LanbonHub) -> None:
        self.hub = hub

    async def async_added_to_hass(self) -> None:
        key = None if self.hub.legacy else getattr(self, "_listen_key", "diagnostic")
        self.async_on_remove(self.hub.async_add_listener(self.async_write_ha_state, key))


class LanbonCaptureSensor(LanbonBaseSensor):
    """Broker capture status; it makes no claim about child-device health."""

    _attr_name = "Capture status"

    def __init__(self, hub: LanbonHub) -> None:
        super().__init__(hub)
        self._attr_unique_id = f"{hub.group_id}_capture" if hub.legacy else "ha_mqtt_capture"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"group:{hub.group_id}")},
            manufacturer="Lanbon",
            name=f"{NAME} {hub.group_id}",
            model="MQTT topic group observer",
        )
        if not hub.legacy:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, "broker:ha_mqtt")},
                manufacturer="Lanbon",
                name=NAME,
                model="MQTT broker observer",
            )

    @property
    def native_value(self) -> str:
        return "listening" if self.hub.store.connected else "disconnected"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        store = self.hub.store
        return {
            "group_id": self.hub.group_id,
            "observed_messages": store.messages,
            "retained_replays": store.retained_messages,
            "unattributed_detail_reports": store.unattributed_details,
            "rejected_messages": store.rejected_messages,
            "connection_generation": store.epoch,
            "device_health_asserted": False,
            "actuator_commands_enabled": self.hub.enable_controls,
            "report_freshness_seconds": self.hub.max_age,
            "scoped_transport_messages": store.transport_messages,
            "report_family_counts": dict(store.family_counts),
            "broker_reports": dict(self.hub.broker_diagnostics),
        }


class LanbonControlStatusSensor(LanbonBaseSensor):
    """Enabled explanation of why this physical device has or lacks controls."""

    _attr_name = "Control status"
    _attr_entity_registry_enabled_default = True

    def __init__(self, hub, device):
        super().__init__(hub)
        self.device = device
        self._attr_unique_id = f"{device.device_id}_device_control_status"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Lanbon",
            name=f"Lanbon {device.device_id}",
        )

    @property
    def native_value(self):
        return self.hub.control_status(self.device.device_id)["status"]

    @property
    def extra_state_attributes(self):
        values = self.hub.control_status(self.device.device_id)
        return {key: value for key, value in values.items() if key != "status"}


class LanbonDetailSensor(LanbonBaseSensor):
    """One bounded diagnostic scalar or complete latest report-family attributes."""

    def __init__(self, hub, device, key):
        super().__init__(hub)
        self.device, self.key = device, key
        self._attr_unique_id = f"{device.device_id}_device_{key}"
        self._attr_name = DETAIL_NAMES.get(
            key,
            {
                "device_details": "Device details",
                "diagnostic_report_received": "Last diagnostic receipt",
            }.get(key),
        )
        self._attr_entity_registry_enabled_default = (
            key in ENABLED_DETAILS or key == "device_details"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Lanbon",
            name=f"Lanbon {device.device_id}",
        )
        if firmware := device.diagnostics.get("firmware_version"):
            self._attr_device_info["sw_version"] = str(firmware.value)
        if (model := device.diagnostics.get("model")) and model.value != "Unresolved type":
            self._attr_device_info["model"] = str(model.value)
        if key == "diagnostic_report_received":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        if key == "network_rssi":
            self._attr_native_unit_of_measurement = "dBm"
        if key in {"timer_delay_duration", "timer_greenMode_duration"}:
            self._attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def _value(self):
        return self.device.diagnostics.get(self.key)

    def _current(self, observation):
        if (
            observation is None
            or not self.hub.store.connected
            or observation.epoch != self.hub.store.epoch
            or self.device.device_id in self.hub.conflicts
        ):
            return False
        limit = (
            self.hub.policy.root_cache_max_age
            if observation.source == "root_cache_query_response"
            else self.hub.max_age
        )
        return 0 <= (dt_util.utcnow() - observation.received).total_seconds() <= limit

    @property
    def available(self):
        if self.key in {"device_details", "diagnostic_report_received"}:
            return bool(self.device.details)
        if self.key in STATIC_DETAILS:
            return self._value() is not None
        return self._current(self._value())

    @property
    def native_value(self):
        if self.key == "device_details":
            return len(self.device.details)
        if self.key == "diagnostic_report_received":
            return max((item.received for item in self.device.details.values()), default=None)
        observation = self._value()
        if observation is None:
            return None
        value = observation.value
        if isinstance(value, dict):
            if self.key.startswith("timer_slot_"):
                return (
                    f"{value['hour']:02}:{value['minute']:02}"
                    if value.get("time_valid")
                    else "invalid reported time"
                )
            return f"object ({len(value)} fields)"
        if isinstance(value, list):
            return len(value)
        if isinstance(value, bool):
            return "on" if value else "off"
        return value[:240] if isinstance(value, str) else value

    @property
    def extra_state_attributes(self):
        common = {"physical_state_verified": False, "peer_observation_age_known": False}
        common["membership_conflict"] = self.device.device_id in self.hub.conflicts
        if self.key in {"device_details", "diagnostic_report_received"}:

            def summarize(item):
                return {
                    "data": item.value,
                    "source": item.source,
                    "confidence": item.confidence,
                    "connection_generation": item.epoch,
                    "capture_current": self._current(item),
                    "peer_age_known": False,
                }

            return common | {
                "reports": {key: summarize(item) for key, item in self.device.details.items()},
                "cached_reports": {
                    key: summarize(item) | {"trusted": False, "capture_current": False}
                    for key, item in self.device.cached_details.items()
                },
            }
        observation = self._value()
        attrs = common | {
            "reported_state_source": observation.source if observation else None,
            "capture_current": self._current(observation),
            "unit_qualification": "raw_or_profile_candidate",
        }
        if self.key in {"firmware_version", "firmware_raw"}:
            attrs["field_confidence"] = "source_verified_version_encoding"
        if self.key in {
            "temperature_decoder_candidate",
            "power_decoder_candidate",
            "energy_decoder_candidate",
        }:
            attrs["unit_qualification"] = "unverified; no measurement capability asserted"
        if observation is not None and isinstance(observation.value, (dict, list)):
            attrs["reported_fields"] = observation.value
        if self.key.startswith("timer_"):
            attrs["countdown_running_asserted"] = False
            attrs["live_firmware_compatibility_verified"] = False
            if self.key in {"timer_delay_duration", "timer_greenMode_duration"}:
                attrs["duration_kind"] = (
                    "remaining_at_receipt"
                    if self.key == "timer_delay_duration"
                    else "configured_inactivity_period"
                )
                attrs["unit_qualification"] = "historical_one_second_counter_loop"
        if self.key.startswith("network_"):
            attrs["namespace"] = "ESP-MDF/Mlink node metadata; not Lanbon output or peer health"
            report = self.device.details.get("network")
            attrs["metadata_role"] = report.value.get("role", "root") if report else None
        return attrs


class LanbonGroupDetailsSensor(LanbonBaseSensor):
    """Group-scoped headers remain on the broker observer, not every child."""

    def __init__(self, hub, group):
        super().__init__(hub)
        self._group_id = group
        self._attr_unique_id = f"ha_mqtt_group_{group}_details"
        self._attr_name = f"Group {group} details"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "broker:ha_mqtt")}, manufacturer="Lanbon", name=NAME
        )

    @property
    def native_value(self):
        return "observed" if self.hub.store.connected else "disconnected"

    @property
    def extra_state_attributes(self):
        return {
            "group_id": self._group_id,
            "report": self.hub.group_diagnostics.get(self._group_id, {}),
            "query_status": self.hub.query_status.get(self._group_id),
            "peer_health_asserted": False,
        }


class LanbonObservationSensor(LanbonBaseSensor):
    """An independent diagnostic observation with cache and freshness metadata."""

    def __init__(
        self,
        hub: LanbonHub,
        device: DeviceObservation,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(hub)
        self.device = device
        self.entity_description = description
        channel = "device" if device.channel is None else f"channel_{device.channel}"
        prefix = f"{hub.group_id}_" if hub.legacy else ""
        self._attr_unique_id = f"{prefix}{device.device_id}_{channel}_{description.key}"
        self._listen_key = (device.device_id, device.channel, description.key)
        if not hub.legacy and description.key == "last_received":
            self._listen_key = "diagnostic"
        if not hub.legacy and description.key in {"output_code", "last_received"}:
            self._attr_entity_registry_enabled_default = False
        if device.channel is not None:
            self._attr_name = f"Channel {device.channel} {description.name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Lanbon",
            name=f"Lanbon {device.device_id}",
            via_device=(DOMAIN, f"group:{hub.group_id}"),
        )
        if not hub.legacy:
            self._attr_device_info.pop("via_device", None)

    def _observation(self) -> FieldObservation | None:
        key = self.entity_description.key
        if key == "last_received":
            if self.device.last_received is None:
                return None
            return FieldObservation(
                self.device.last_received,
                self.device.last_received,
                self.device.last_epoch,
                "",
                "receipt_time",
            )
        return self.device.fields.get(key)

    @property
    def available(self) -> bool:
        if not self.hub.legacy and self.entity_description.key != "last_received":
            return self.hub.observation(self.device, self.entity_description.key) is not None
        return self.hub.store.available(self._observation(), dt_util.utcnow(), self.hub.max_age)

    @property
    def native_value(self) -> str | int | float | datetime | None:
        if not self.hub.legacy and self.entity_description.key != "last_received":
            observation = self.hub.observation(self.device, self.entity_description.key)
            return observation.value if observation else None
        observation = self._observation()
        return None if observation is None else observation.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.hub.legacy:
            observation = self.hub.observation(self.device, self.entity_description.key)
            return {
                "reported_state_source": observation.source.value if observation else None,
                "peer_observation_age_known": False,
                "physical_state_verified": False,
                "firmware_version": self.device.detail.get("firmware_version"),
                "device_type": self.hub.device_type(self.device.device_id),
            }
        key = self.entity_description.key
        observation = self._observation()
        cached = self.device.cache.get(key)
        now = dt_util.utcnow()

        def metadata_attributes(values: dict[str, FieldObservation]) -> dict[str, Any]:
            return {
                name: {
                    "value": item.value,
                    "received_utc": item.received.isoformat(),
                    "source_topic": item.topic,
                    "confidence": item.confidence,
                    "retained": item.retained,
                    "capture_current": (
                        not item.retained and self.hub.store.available(item, now, self.hub.max_age)
                    ),
                }
                for name, item in values.items()
            }

        return {
            "device_id": self.device.device_id,
            "channel": self.device.channel,
            "field_confidence": observation.confidence if observation else None,
            "source_topic": observation.topic if observation else None,
            "received_utc": observation.received.isoformat() if observation else None,
            "capture_current": self.available,
            "cached_replay_value": cached.value if cached else None,
            "cached_replay_received_utc": cached.received.isoformat() if cached else None,
            "cached_replay_trusted": False,
            "physical_state_verified": False,
            "firmware_raw": self.device.detail.get("firmware_raw"),
            "firmware_version": self.device.detail.get("firmware_version"),
            "firmware_confidence": self.device.detail.get("firmware_confidence"),
            "device_type": self.device.detail.get("device_type"),
            "candidate_model": self.device.detail.get("candidate_model"),
            "model_confidence": self.device.detail.get("model_confidence"),
            "native_channel_bytes": self.device.detail.get("native_channel_bytes"),
            "metadata_provenance": metadata_attributes(self.device.metadata.live),
            "cached_metadata": metadata_attributes(self.device.metadata.cache),
            "cached_metadata_trusted": False,
        }
