"""Bounded, passive Lanbon discovery using the existing MQTT connection.

Candidates are configuration hints, never evidence of physical output or health.
Home Assistant is imported only inside the transport function.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .protocol import parse_message

MAX_GROUPS = 16
MAX_PEERS_PER_GROUP = 16
MAX_PACKET_BYTES = 8192
MAX_TOPIC_BYTES = 512
ENERGY_TOPIC = "lanbon/a/L8devElecsumReport"
_ID = r"[0-9A-Fa-f]{12}"
_NATIVE = re.compile(
    rf"^(?P<prefix>.+)/(?P<group>{_ID})/(?P<component>light|switch)/"
    rf"(?P<peer>{_ID})(?:-(?P<channel>0[1-4]))?/(?P<suffix>config|state|brightnessState)$"
)


def normalize_group_id(value: str) -> str:
    """Normalize a mesh/topic ID without equating it with a physical root MAC."""
    if not isinstance(value, str):
        raise ValueError("invalid_group_id")
    result = value.strip().replace(":", "").replace("-", "").upper()
    if not re.fullmatch(_ID, result) or result in {"000000000000", "FFFFFFFFFFFF"}:
        raise ValueError("invalid_group_id")
    return result


def normalize_prefix(value: str) -> str:
    """Use literal portable levels shared by the native command constructor."""
    if not isinstance(value, str):
        raise ValueError("invalid_prefix")
    result = value.strip().strip("/")
    if len(result) > 128 or not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", result):
        raise ValueError("invalid_prefix")
    return result


def normalize_groups(values: str | list[str] | tuple[str, ...]) -> list[str]:
    """Accept a bounded list or comma/newline separated manual identifiers."""
    if isinstance(values, str):
        if len(values) > 1024:
            raise ValueError("invalid_group_id")
        values = [part for part in re.split(r"[,;\s]+", values.strip()) if part]
    if not isinstance(values, (list, tuple)) or len(values) > MAX_GROUPS:
        raise ValueError("too_many_groups")
    groups = list(dict.fromkeys(normalize_group_id(value) for value in values))
    if not groups:
        raise ValueError("no_groups")
    return groups


def _peer(value: str) -> str | None:
    try:
        result = normalize_group_id(value)
    except ValueError:
        return None
    return result if not int(result[:2], 16) & 1 else None


def native_config_hint(topic: str, payload: bytes, prefix: str | None = None) -> dict | None:
    """Require Lanbon identity and its expected native config topic namespace.

    Generic MQTT lights and switches must not start this integration's flow.
    Even a valid retained config is only a hint until user confirmation.
    """
    if (
        not isinstance(topic, str)
        or len(topic.encode("utf-8")) > MAX_TOPIC_BYTES
        or not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_PACKET_BYTES
        or (match := _NATIVE.fullmatch(topic)) is None
        or match["suffix"] != "config"
    ):
        return None
    try:
        actual_prefix = normalize_prefix(match["prefix"])
        group = normalize_group_id(match["group"])
        document = json.loads(payload)
    except ValueError, UnicodeError, RecursionError:
        return None
    peer = _peer(match["peer"])
    if peer is None or (prefix is not None and actual_prefix != prefix):
        return None
    if not isinstance(document, dict):
        return None
    device = document.get("device", document.get("dev", {}))
    manufacturer = (
        device.get("manufacturer", device.get("mf")) if isinstance(device, dict) else None
    )
    unique_id = document.get("unique_id", document.get("uniq_id"))
    vendor = isinstance(manufacturer, str) and manufacturer.strip().casefold() == "lanbon"
    lanbon_id = isinstance(unique_id, str) and re.match(r"(?i)^lanbon(?:[_:.\-]|$)", unique_id)
    if not (vendor or lanbon_id):
        return None
    base = topic.rsplit("/", 1)[0]
    for names, suffix in (
        (("state_topic", "stat_t"), "state"),
        (("command_topic", "cmd_t"), "set"),
        (("brightness_state_topic", "bri_stat_t"), "brightnessState"),
        (("brightness_command_topic", "bri_cmd_t"), "brightnessSet"),
    ):
        if any(name in document and document[name] != f"{base}/{suffix}" for name in names):
            return None
    return {"group_id": group, "device_id": peer, "discovery_prefix": actual_prefix}


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A bounded summary without packet bodies or household-specific defaults."""

    group_id: str
    current_seen: bool
    current_energy: bool
    cached_seen: bool
    peers: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def label(self) -> str:
        """Expose the quality of a hint in the selection form."""
        source = (
            "current energy report"
            if self.current_energy
            else "current MQTT hint"
            if self.current_seen
            else "retained hint"
        )
        return f"{self.group_id} — {source}; {len(self.peers)} peer hints"


@dataclass
class _Candidate:
    group_id: str
    rank: int = 0
    cached_seen: bool = False
    peers: dict[str, int] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class DiscoveryResult:
    """An empty scan does not prove no devices exist."""

    candidates: tuple[DiscoveryCandidate, ...]
    error: str | None = None


class DiscoveryCollector:
    """Keep at most sixteen groups and sixteen peer hints per group.

    Higher quality current evidence evicts lower quality hints at capacity.
    A retained flood cannot occupy slots reserved by current evidence.
    """

    def __init__(self, prefix: str = "homeassistant", *, max_groups: int = MAX_GROUPS) -> None:
        self.prefix = normalize_prefix(prefix)
        if (
            isinstance(max_groups, bool)
            or not isinstance(max_groups, int)
            or not 1 <= max_groups <= 16
        ):
            raise ValueError("Invalid discovery group bound")
        self.max_groups = max_groups
        self._groups: dict[str, _Candidate] = {}

    def accept(self, topic: str, payload: bytes, *, retained: bool = False) -> bool:
        """Accept known shapes; commands and bespoke bridges do not qualify."""
        if (
            not isinstance(topic, str)
            or len(topic.encode("utf-8")) > MAX_TOPIC_BYTES
            or not isinstance(payload, bytes)
            or len(payload) > MAX_PACKET_BYTES
            or not isinstance(retained, bool)
        ):
            return False
        hint = native_config_hint(topic, payload, self.prefix)
        event = None if hint else parse_message(topic, payload)
        if hint:
            group, peers = hint["group_id"], [hint["device_id"]]
            source, strength = "native_config", 1
        elif event and event["kind"] == "energy_structure" and topic == ENERGY_TOPIC:
            group = event["group_id"]
            peers = [device["device_id"] for device in event["devices"]]
            source, strength = "energy_header", 3
        elif event and event["kind"] in {"power", "brightness"}:
            if event.get("discovery_prefix") != self.prefix:
                return False
            group, peers = event["group_id"], [event["device_id"]]
            source, strength = "native_feedback", 1
        elif event and event["kind"] in {"status", "status_inventory"}:
            group = event["group_id"]
            peers = (
                [device["device_id"] for device in event["devices"]]
                if "devices" in event
                else [event["device_id"]]
            )
            source, strength = "scoped_binary_report", 2
        else:
            return False
        try:
            group = normalize_group_id(group)
        except ValueError:
            return False
        valid_peers = {_peer(peer) for peer in peers}
        valid_peers.discard(None)
        if not valid_peers:
            return False
        rank = 0 if retained else strength
        candidate = self._groups.get(group)
        if candidate is None:
            if len(self._groups) >= self.max_groups:
                weakest = min(self._groups.values(), key=lambda item: item.rank)
                if rank <= weakest.rank:
                    return False
                del self._groups[weakest.group_id]
            candidate = self._groups[group] = _Candidate(group)
        candidate.rank = max(candidate.rank, rank)
        candidate.cached_seen |= retained
        candidate.sources.add(source)
        for peer in sorted(valid_peers):
            if peer in candidate.peers:
                candidate.peers[peer] = max(candidate.peers[peer], rank)
                continue
            if len(candidate.peers) >= MAX_PEERS_PER_GROUP:
                weakest_peer = min(candidate.peers, key=candidate.peers.get)
                if rank <= candidate.peers[weakest_peer]:
                    continue
                del candidate.peers[weakest_peer]
            candidate.peers[peer] = rank
        return True

    def result(self, error: str | None = None) -> DiscoveryResult:
        """Copy summaries so callers cannot mutate the bounded collector."""
        return DiscoveryResult(
            tuple(
                DiscoveryCandidate(
                    group_id=item.group_id,
                    current_seen=item.rank > 0,
                    current_energy=item.rank == 3,
                    cached_seen=item.cached_seen,
                    peers=tuple(sorted(item.peers)),
                    sources=tuple(sorted(item.sources)),
                )
                for item in sorted(
                    self._groups.values(), key=lambda item: (-item.rank, item.group_id)
                )
            ),
            error,
        )


def subscription_topics(prefix: str) -> tuple[str, ...]:
    """Use report namespaces rather than a subscription to the whole broker."""
    prefix = normalize_prefix(prefix)
    return (
        ENERGY_TOPIC,
        f"{prefix}/+/light/+/config",
        f"{prefix}/+/switch/+/config",
        f"{prefix}/+/light/+/state",
        f"{prefix}/+/switch/+/state",
        f"{prefix}/+/light/+/brightnessState",
        "lanbon/a/+/s2m/cmdStatusSynchronous",
        "lanbon/a/+/s2m/cmdStatusGet",
    )


async def async_scan(
    hass: Any,
    prefix: str,
    *,
    seconds: float = 35,
    collector: DiscoveryCollector | None = None,
) -> DiscoveryResult:
    """Listen only; unsubscribe on completion, errors and flow cancellation.

    Subscription registration is not a SUBACK guarantee. A quiet timeout falls
    back to manual configuration instead of asserting that no devices exist.
    """
    from homeassistant.components import mqtt
    from homeassistant.exceptions import HomeAssistantError

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("Invalid scan duration")
    if not math.isfinite(seconds) or not 0 < seconds <= 60:
        raise ValueError("Invalid scan duration")
    collector = collector if collector is not None else DiscoveryCollector(prefix)
    if collector.prefix != normalize_prefix(prefix):
        raise ValueError("Collector prefix mismatch")
    unsubscribers: list[Callable[[], None]] = []
    active = True

    def received(message: Any) -> None:
        if active:
            collector.accept(message.topic, message.payload, retained=message.retain)

    try:
        if not mqtt.is_connected(hass):
            return collector.result("mqtt_unavailable")
        for topic in subscription_topics(prefix):
            unsubscribers.append(
                await mqtt.async_subscribe(hass, topic, received, qos=0, encoding=None)
            )
        await asyncio.sleep(seconds)
        return collector.result(None if mqtt.is_connected(hass) else "mqtt_unavailable")
    except HomeAssistantError, KeyError, ConnectionError:
        return collector.result("mqtt_unavailable")
    finally:
        active = False
        for unsubscribe in reversed(unsubscribers):
            unsubscribe()
