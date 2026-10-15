"""Pure observation state; caches and live reports have distinct trust."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class FieldObservation:
    """One field with receipt time and broker connection generation."""

    value: Any
    received: datetime
    epoch: int
    topic: str
    confidence: str
    retained: bool = False
    source: str = "unsolicited_mqtt_report"


def _confidence_rank(confidence: str) -> int:
    """Prevent a weaker interpretation from replacing verified metadata."""
    if confidence.startswith("source_verified"):
        return 3
    if confidence in {"observed", "live_extension_matches_firmware_type_enum"}:
        return 2
    return 0 if confidence == "unverified" else 1


@dataclass(slots=True)
class MetadataObservations:
    """Live metadata and retained bootstrap values keep separate provenance."""

    live: dict[str, FieldObservation] = field(default_factory=dict)
    cache: dict[str, FieldObservation] = field(default_factory=dict)

    def merge(
        self,
        values: dict[str, Any],
        *,
        retained: bool,
        received: datetime,
        epoch: int,
        topic: str,
        confidence: str,
    ) -> None:
        target = self.cache if retained else self.live
        for key, value in values.items():
            if value is None or key == "device_id":
                continue
            existing = target.get(key)
            if existing is not None and (
                received < existing.received
                or _confidence_rank(confidence) < _confidence_rank(existing.confidence)
            ):
                continue
            target[key] = FieldObservation(value, received, epoch, topic, confidence, retained)


@dataclass(slots=True)
class DeviceObservation:
    """A device/channel's independent observed and cached fields."""

    device_id: str
    channel: int | None = None
    fields: dict[str, FieldObservation] = field(default_factory=dict)
    cache: dict[str, FieldObservation] = field(default_factory=dict)
    metadata: MetadataObservations = field(default_factory=MetadataObservations)
    last_received: datetime | None = None
    last_epoch: int = -1
    group_id: str | None = None
    diagnostics: dict[str, FieldObservation] = field(default_factory=dict)
    details: dict[str, FieldObservation] = field(default_factory=dict)
    cached_details: dict[str, FieldObservation] = field(default_factory=dict)

    @property
    def detail(self) -> dict[str, Any]:
        """Convenient live-only metadata values; retained data never fills these."""
        return {key: observation.value for key, observation in self.metadata.live.items()}


def bounded_details(value: Any, *, depth: int = 0) -> Any:
    """Reject oversized/unserializable report shapes; never retain arbitrary objects."""
    if depth > 8:
        raise ValueError("Diagnostic nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite diagnostic")
        return value
    if isinstance(value, str):
        if len(value) > 512:
            raise ValueError("Diagnostic string limit")
        return value
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("Diagnostic list limit")
        return [bounded_details(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64 or any(not isinstance(key, str) or len(key) > 64 for key in value):
            raise ValueError("Diagnostic map limit")
        return {key: bounded_details(item, depth=depth + 1) for key, item in value.items()}
    raise ValueError("Unsupported diagnostic value")


class ObservationStore:
    """Accept reports for one configured topic group without actuator effects."""

    def __init__(self, group_id: str, max_devices: int = 256, *, groups=None) -> None:
        self.group_id = group_id
        self.groups = set(groups or {group_id})
        self.max_devices = max_devices
        self.connected = False
        self.epoch = 0
        self.devices: dict[tuple[str, int | None], DeviceObservation] = {}
        self.pending_details: dict[str, MetadataObservations] = {}
        self.messages = 0
        self.retained_messages = 0
        self.unattributed_details = 0
        self.rejected_messages = 0
        self.transport_messages = 0
        self.family_counts: dict[str, int] = {}

    def record_details(
        self,
        peer,
        group,
        family,
        data,
        values,
        *,
        received,
        source,
        confidence,
        retained=False,
        epoch=None,
        allocate=True,
    ) -> bool:
        """Keep one bounded report per known family; caches never allocate live peers."""
        if group not in self.groups or not isinstance(family, str) or len(family) > 48:
            return False
        data = bounded_details(data)
        if len(json.dumps(data, separators=(",", ":"))) > 8192:
            raise ValueError("Diagnostic family byte limit")
        device = self.devices.get((peer, None))
        if device is None and (retained or not allocate):
            return False
        if device is None:
            device = self._device(peer, None)
        if device is None or (device.group_id is not None and device.group_id != group):
            return False
        device.group_id = group
        target = device.cached_details if retained else device.details
        if family not in target and len(target) >= 16:
            return False
        generation = self.epoch if epoch is None else epoch
        target[family] = FieldObservation(
            data, received, generation, "", confidence, retained, source
        )
        if retained:
            return True
        for key, value in values.items():
            if len(key) > 64:
                continue
            if value is None:
                device.diagnostics.pop(key, None)
                continue
            value = bounded_details(value)
            if key not in device.diagnostics and len(device.diagnostics) >= 64:
                continue
            device.diagnostics[key] = FieldObservation(
                value, received, generation, "", confidence, False, source
            )
        return True

    def set_connected(self, connected: bool) -> None:
        """Each transport transition invalidates the previous generation."""
        if self.connected != connected:
            self.epoch += 1
            self.connected = connected

    def _device(self, device_id: str, channel: int | None) -> DeviceObservation | None:
        key = (device_id, channel)
        if key not in self.devices:
            if len(self.devices) >= self.max_devices:
                self.rejected_messages += 1
                return None
            pending = self.pending_details.get(device_id, MetadataObservations())
            self.devices[key] = DeviceObservation(
                device_id,
                channel,
                metadata=MetadataObservations(pending.live.copy(), pending.cache.copy()),
            )
        return self.devices[key]

    def accept(
        self,
        event: dict[str, Any],
        *,
        retained: bool,
        received: datetime,
        update_fields: bool = True,
    ) -> bool:
        """Apply a parser event; return whether a device's diagnostics changed."""
        if not self.connected:
            return False
        group = event.get("group_id")
        if group is not None and group not in self.groups:
            self.rejected_messages += 1
            return False
        self.messages += 1
        self.retained_messages += retained
        if event.get("kind") == "detail":
            # Global detail reports cannot establish a device's group membership.
            self.unattributed_details += group is None
            for row in event.get("devices", []):
                device_id = row.get("device_id")
                if not isinstance(device_id, str):
                    continue
                if (
                    device_id not in self.pending_details
                    and len(self.pending_details) >= self.max_devices
                ):
                    continue
                metadata = self.pending_details.setdefault(device_id, MetadataObservations())
                merge_args = {
                    "retained": retained,
                    "received": received,
                    "epoch": self.epoch,
                    "topic": event["topic"],
                    "confidence": row.get(
                        "firmware_confidence", event.get("confidence", "structural")
                    ),
                }
                metadata.merge(row, **merge_args)
                for key, device in self.devices.items():
                    if key[0] == device_id:
                        device.metadata.merge(row, **merge_args)
            return True
        if event.get("kind") == "status_inventory" and group in self.groups:
            for row in event.get("devices", []):
                device_id = row.get("device_id")
                if not isinstance(device_id, str):
                    continue
                device = self._device(device_id, None)
                if device is None:
                    continue
                model = row.get("model")
                device.group_id = group
                if model == "Unresolved type":
                    model = None
                model_confidence = row.get(
                    "model_confidence",
                    "source_verified_type_enum" if model is not None else "structural",
                )
                device.metadata.merge(
                    {
                        "device_type": row.get("device_type"),
                        "candidate_model": model,
                        "model_confidence": model_confidence,
                    },
                    retained=retained,
                    received=received,
                    epoch=self.epoch,
                    topic=event["topic"],
                    confidence=model_confidence,
                )
                if (code := row.get("status_raw")) is None:
                    continue
                if not update_fields:
                    continue
                observation = FieldObservation(
                    code, received, self.epoch, event["topic"], event["confidence"], retained
                )
                if retained:
                    device.cache["output_code"] = observation
                else:
                    device.fields["output_code"] = observation
                    device.last_received = received
                    device.last_epoch = self.epoch
            return True
        if group not in self.groups or event.get("kind") not in {"power", "brightness", "status"}:
            return False
        device_id = event.get("device_id")
        if not isinstance(device_id, str):
            return False
        device = self._device(device_id, event.get("channel"))
        if device is None:
            return False
        values = event.get("fields", {})
        device.group_id = group
        if event["kind"] == "status":
            # Native semantics remain structural in the generic alpha.
            marker_confidence = values.get("marker_confidence", "unverified")
            device.metadata.merge(
                {
                    "device_type": values.get("candidate_device_type"),
                    "candidate_model": values.get("candidate_model"),
                    "model_confidence": marker_confidence,
                },
                retained=retained,
                received=received,
                epoch=self.epoch,
                topic=event["topic"],
                confidence=marker_confidence,
            )
            device.metadata.merge(
                {"native_channel_bytes": values.get("channel_bytes")},
                retained=retained,
                received=received,
                epoch=self.epoch,
                topic=event["topic"],
                confidence=event["confidence"],
            )
            values = {"output_code": values.get("output_code")}
        else:
            metric = event["kind"]
            values = {metric: values.get(metric)}
        if not update_fields:
            return True
        for metric, value in values.items():
            if value is None:
                continue
            observation = FieldObservation(
                value, received, self.epoch, event["topic"], event["confidence"], retained
            )
            if retained:
                device.cache[metric] = observation
            else:
                device.fields[metric] = observation
                device.last_received = received
                device.last_epoch = self.epoch
        return True

    def available(self, observation: FieldObservation | None, now: datetime, max_age: int) -> bool:
        """Availability means recent report capture, not physical connectivity."""
        return bool(
            self.connected
            and observation is not None
            and observation.epoch == self.epoch
            and 0 <= (now - observation.received).total_seconds() <= max_age
        )
