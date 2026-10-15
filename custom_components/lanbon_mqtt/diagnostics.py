"""Bounded diagnostic reports with household identity and address redaction."""

import re

_SENSITIVE = {
    "device_id",
    "group_id",
    "root_mac",
    "self_mac",
    "mesh_id",
    "parent_mac",
    "ip_address",
    "gateway_address",
    "native_base",
    "topic",
    "host",
    "name",
}


def _redact(value, identifiers):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in _SENSITIVE:
                result[key] = "**REDACTED**"
            elif key.removeprefix("network_") in _SENSITIVE:
                result[key] = (
                    _redact(item | {"value": "**REDACTED**"}, identifiers)
                    if isinstance(item, dict)
                    else "**REDACTED**"
                )
            else:
                result[key] = _redact(item, identifiers)
        return result
    if isinstance(value, list):
        return [_redact(item, identifiers) for item in value]
    if isinstance(value, str):
        if len(value) >= 12 and re.fullmatch(r"[0-9a-fA-F]+", value):
            return "**REDACTED_RAW**"
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", value):
            return "**REDACTED**"
        for identifier in identifiers:
            value = re.sub(re.escape(identifier), "**REDACTED**", value, flags=re.IGNORECASE)
        return value
    return value


async def async_get_config_entry_diagnostics(hass, entry):
    hub = entry.runtime_data
    identities = set(hub.groups) | set(hub.bindings.peer_owners)
    identities |= {
        ":".join(identity[i : i + 2] for i in range(0, 12, 2)) for identity in tuple(identities)
    }
    devices = []
    for (_, channel), device in hub.store.devices.items():
        if channel is not None:
            continue

        def report(item):
            return {
                "value": item.value,
                "source": item.source,
                "received_utc": item.received.isoformat(),
                "connection_generation": item.epoch,
                "confidence": item.confidence,
                "retained": item.retained,
            }

        devices.append(
            {
                "diagnostic_values": {
                    key: report(value) for key, value in device.diagnostics.items()
                },
                "reports": {key: report(value) for key, value in device.details.items()},
                "cached_reports": {
                    key: report(value) for key, value in device.cached_details.items()
                },
            }
        )
    return {
        "groups": len(hub.groups),
        "devices": len(hub.store.devices),
        "connected": hub.store.connected,
        "controls_enabled": hub.enable_controls,
        "conflicts": len(hub.conflicts),
        "observed_messages": hub.store.messages,
        "rejected_messages": hub.store.rejected_messages,
        "query_status": list(hub.query_status.values()),
        "physical_state_verified": False,
        "peer_observation_age_known": False,
        "scoped_transport_messages": hub.store.transport_messages,
        "report_family_counts": dict(hub.store.family_counts),
        "network_status": list(getattr(hub, "network_status", {}).values()),
        "device_diagnostics": _redact(devices, identities),
        "group_diagnostics": [
            _redact(value, identities) for value in hub.group_diagnostics.values()
        ],
    }
