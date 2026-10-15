"""Portable Lanbon L8 parsing and pure command construction; no networking."""

from __future__ import annotations

import hashlib
import re

MAX_PAYLOAD = 65536
MAC = r"[0-9A-Fa-f]{12}"
TEXT_TOPIC = re.compile(
    rf"^(?P<prefix>.+)/(?P<group>{MAC})/(?P<domain>light|switch)/"
    rf"(?P<device>{MAC})(?:-(?P<channel>[0-9]{{2}}))?/(?P<suffix>[^/]+)$"
)
BINARY_TOPIC = re.compile(rf"^lanbon/[ab]/MAC:(?P<group>{MAC})/(?P<suffix>.+)$")
PERCENT = re.compile(rb"(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")
MODEL_NAMES = {0x4A: "1-gang relay", 0x4B: "2-gang relay", 0x4C: "3-gang relay", 0x4D: "Dimmer"}
DIMMER_TYPES = {4, 60, 66, 77}  # Vendor APK's dimmer UI predicate.
STATUS_CHUNK_PROFILES = {"stock_44": 44}
PREFIX = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


def interpret_output(
    device_type: int, status_raw: int, *, context: str = "inventory", qualified: bool = False
) -> dict:
    """Explain a type-qualified status using the inspected legacy source profile.

    This is reported output, not a physical measurement or feedback guarantee.
    Unknown types stay unknown; a sync extension must first be verified as type.
    """
    if context not in {"inventory", "sync"}:
        raise ValueError("Unsupported output context")
    if not isinstance(qualified, bool):
        raise ValueError("Output qualification must be explicitly boolean")
    if (
        isinstance(status_raw, bool)
        or not isinstance(status_raw, int)
        or not 0 <= status_raw <= 255
    ):
        raise ValueError("Output must be one byte")
    result = {
        "profile": "vendor_apk_4.0.21_and_stock_1.1.13",
        "context": context,
        "raw": status_raw,
        "physical_state_verified": False,
    }
    if context == "sync" and not qualified:
        return result | {"interpretation": "unqualified_sync_profile"}
    if device_type in {74, 75, 76}:
        if context == "sync":
            return result | {"interpretation": "relay_sync_requires_native_channel_feedback"}
        result["channels"] = [
            {
                "channel": channel + 1,
                "reported_power": "ON" if status_raw & (1 << channel) else "OFF",
                "changed_flag_raw": bool(status_raw & (1 << (channel + 5))),
                "changed_flag_qualified": False,
            }
            for channel in range(device_type - 73)
        ]
        channel_mask = (1 << (device_type - 73)) - 1
        result["uninterpreted_bits"] = status_raw & ~channel_mask & 255
    elif device_type in DIMMER_TYPES:
        if context == "inventory" and device_type != 77:
            return result | {"interpretation": "unsupported_inventory_profile"}
        level = status_raw & 0x7F if context == "sync" else status_raw
        result.update(
            output_level=level,
            high_bit=bool(status_raw & 0x80),
            high_bit_semantics="unqualified",
        )
        # The UI's 7-bit level is wider than the native HA percent field.
        # Preserve out-of-percent-range values rather than clamping or scaling.
        if device_type == 77 and level <= 100:
            # Output percentage can be zero at OFF; remembered brightness is
            # a separate plaintext report and must not be overwritten by this.
            result["output_percent"] = level
            result["reported_power"] = "ON" if level else "OFF"
        elif context == "sync" and device_type != 77:
            result["reported_power"] = "ON" if level else "OFF"
        else:
            result["interpretation"] = "outside_qualified_percent_range"
    else:
        result["interpretation"] = "unsupported_device_type"
    return result


def _measurement(raw: bytes, *, bias: int = 0) -> float | None:
    """APK decimal triplet: big-endian integer + hundredths, optional bias."""
    if raw[2] > 99:
        return None
    return (int.from_bytes(raw[:2], "big") - bias) + raw[2] / 100


def _identifier(value: str, *, physical: bool = False) -> str:
    if not isinstance(value, str) or not re.fullmatch(MAC, value):
        raise ValueError("Identifier must be twelve hexadecimal digits")
    value = value.upper()
    if value in {"000000000000", "FFFFFFFFFFFF"} or (physical and int(value[:2], 16) & 1):
        raise ValueError("Unspecified, broadcast or multicast physical identifier")
    return value


def control_profile(device_type: int) -> dict | None:
    """Describe only supported output families; this never qualifies a device."""
    if (
        isinstance(device_type, bool)
        or not isinstance(device_type, int)
        or device_type not in MODEL_NAMES
    ):
        return None
    dimmer = device_type == 0x4D
    return {
        "device_type": device_type,
        "component": "light" if dimmer else "switch",
        "channels": 1 if dimmer else device_type - 0x49,
        "brightness": dimmer,
        "requires_observed_native_route": not dimmer,
        "physical_delivery_verified": False,
    }


def parameter_getter_messages(
    group_id: str, device_id: str, device_type: int
) -> tuple[tuple[str, bytes], ...]:
    """Construct fixed nonactuating legacy getters; caller owns explicit intent.

    Current ownership/type, request windows, rate limits, QoS0 and retainFalse
    belong to the transport consumer. Opcode0x12 and all setters are excluded.
    """
    group = _identifier(group_id)
    peer = _identifier(device_id, physical=True)
    if type(device_type) is not int or device_type not in {0x4A, 0x4B, 0x4C, 0x4D}:
        raise ValueError("Parameter getter profile is not qualified for this type")
    topic = f"lanbon/a/MAC:{group}/m2s/cmdQuery"
    target = bytes.fromhex(peer)
    return tuple((topic, bytes([opcode]) + target + b"\x00") for opcode in (0xA0, 0xA1, 0xA2, 0xA3))


def native_control_message(
    group_id: str,
    device_id: str,
    device_type: int,
    action: str,
    value=None,
    channel: int | None = None,
    discovery_prefix: str = "homeassistant",
    *,
    observed_native_base: str | None = None,
    component: str | None = None,
) -> tuple[str, bytes]:
    """Construct a native topic/payload after explicit route/profile selection.

    Relay routes must come from observed native feedback/config. Some firmware
    uses suffixless light routes even for a single relay. No route is learned
    here and no packet is sent. Publishing/qualification belongs to the caller.
    """
    group_id, device_id = _identifier(group_id), _identifier(device_id, physical=True)
    profile = control_profile(device_type)
    if profile is None:
        raise ValueError("Unsupported control profile")
    if (
        not isinstance(discovery_prefix, str)
        or len(discovery_prefix) > 128
        or not PREFIX.fullmatch(discovery_prefix)
    ):
        raise ValueError("Invalid native discovery prefix")
    if component is not None and (
        not isinstance(component, str) or component not in {"light", "switch"}
    ):
        raise ValueError("Unsupported native component")
    if isinstance(channel, bool) or (channel is not None and not isinstance(channel, int)):
        raise ValueError("Invalid control channel")
    if device_type == 0x4D:
        if channel is not None:
            raise ValueError("Dimmer is a device output, not a numbered gang")
    else:
        channel = 1 if channel is None and profile["channels"] == 1 else channel
        if channel is None or not 1 <= channel <= profile["channels"]:
            raise ValueError("Relay channel outside selected profile")
        if observed_native_base is None:
            raise ValueError("Relay control requires an observed native route")
    if observed_native_base is not None:
        if not isinstance(observed_native_base, str) or len(observed_native_base) > 256:
            raise ValueError("Invalid observed native route")
        route = TEXT_TOPIC.fullmatch(observed_native_base + "/state")
        if route is None or route["prefix"] != discovery_prefix:
            raise ValueError("Observed native route is outside configured prefix")
        if route["group"].upper() != group_id or route["device"].upper() != device_id:
            raise ValueError("Observed native route has a different group or peer")
        route_channel = int(route["channel"]) if route["channel"] else None
        if device_type == 0x4D:
            if route_channel is not None or route["domain"] != "light":
                raise ValueError("Dimmer route must be an unnumbered native light")
        elif route_channel != channel and not (
            profile["channels"] == 1 and channel == 1 and route_channel is None
        ):
            raise ValueError("Observed native route has a different channel")
        if component is not None and component != route["domain"]:
            raise ValueError("Observed native component differs from selected component")
        native_base = observed_native_base
        component = route["domain"]
    else:
        if component not in {None, "light"}:
            raise ValueError("Qualified dimmer profile requires native light component")
        component = "light"
        native_base = f"{discovery_prefix}/{group_id}/light/{device_id}"
    if action == "power" and isinstance(value, str) and value in {"ON", "OFF"}:
        return native_base + "/set", value.encode("ascii")
    if action == "brightness" and device_type == 0x4D and component == "light":
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            raise ValueError("Brightness must be an integer percentage from zero to one hundred")
        return native_base + "/brightnessSet", str(value).encode("ascii")
    raise ValueError("Action is not supported by the selected profile")


def version_string(value: int) -> str:
    """Firmware integer major*10000 + minor*100 + patch, source-confirmed."""
    return f"{value // 10000}.{value % 10000 // 100}.{value % 100}"


def _base(topic: str, payload: bytes, kind: str, confidence="structural") -> dict:
    return {
        "kind": kind,
        "topic": topic,
        "confidence": confidence,
        "fields": {},
        "raw": {
            "length": len(payload),
            "hex": payload.hex(),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }


def _detail(topic: str, payload: bytes) -> dict | None:
    if (
        len(payload) < 16
        or payload[:2] != b"\x01\x08"
        or any(payload[3:16])
        or len(payload) != 16 + payload[2] * 8
    ):
        return None
    devices = []
    seen = set()
    for offset in range(16, len(payload), 8):
        peer = payload[offset : offset + 6].hex().upper()
        if peer in seen:
            return None
        seen.add(peer)
        firmware_raw = int.from_bytes(payload[offset + 6 : offset + 8], "big")
        devices.append(
            {
                "device_id": peer,
                "firmware_raw": firmware_raw,
                "firmware_version": version_string(firmware_raw),
                "firmware_confidence": "source_verified_encoding",
                "raw_hex": payload[offset : offset + 8].hex(),
            }
        )
    event = _base(topic, payload, "detail", "observed")
    event.update(
        devices=devices,
        count=payload[2],
        fields={"format_version": payload[0], "record_size": payload[1]},
    )
    return event


def _energy(topic: str, payload: bytes) -> dict | None:
    # The next two bytes are timezone H/M, not a fixed format-version magic.
    if len(payload) < 18 or not 1 <= payload[0] <= 98 or len(payload) != 9 + payload[0] * 9:
        return None
    peers = [payload[o : o + 6].hex().upper() for o in range(9, len(payload), 9)]
    if len(set(peers)) != len(peers):
        return None
    try:
        group_id = _identifier(payload[3:9].hex())
    except ValueError:
        return None
    event = _base(topic, payload, "energy_structure")
    event.update(
        group_id=group_id,
        group_id_basis="payload_bssid_or_mesh_id",
        timezone_raw_hex=payload[1:3].hex(),
        root_mac=None,
        root_mac_available=False,
        peer_health_asserted=False,
        measurement_entities_eligible=False,
        count=payload[0],
        devices=[
            {
                "device_id": payload[o : o + 6].hex().upper(),
                "measurement_hex": payload[o + 6 : o + 9].hex(),
                "units": "unverified",
                "unit": None,
                "measurement_candidate": _measurement(payload[o + 6 : o + 9]),
                "measurement_entities_eligible": False,
                "historical_counter_profile": "hour_window_not_cumulative_local_record",
                "counter_reset_qualified": False,
            }
            for o in range(9, len(payload), 9)
        ],
    )
    return event


def _status(topic: str, payload: bytes, group_id: str, *, qualified_sync=False) -> dict | None:
    if len(payload) not in {11, 12}:
        return None
    # Both stock firmware and vendor APK confirm the first 11 bytes. The live
    # 12-byte extension matches a known type code; its semantics stay labelled.
    event = _base(topic, payload, "status")
    marker = payload[11] if len(payload) == 12 else None
    event.update(
        group_id=group_id,
        device_id=payload[5:11].hex().upper(),
        fields={
            "output_code": payload[0],
            "channel_bytes": list(payload[:5]),
            "auxiliary_raw_hex": payload[1:5].hex(),
            "marker": marker,
            "candidate_device_type": marker,
            "candidate_model": MODEL_NAMES.get(marker),
            "marker_confidence": "live_extension_matches_firmware_type_enum"
            if marker in MODEL_NAMES
            else "unverified",
        },
    )
    if marker in MODEL_NAMES:
        event["candidate_profile"] = interpret_output(
            marker, payload[0], context="sync", qualified=qualified_sync
        )
        event["candidate_profile"]["confidence"] = (
            "explicitly_selected_sync_profile"
            if qualified_sync
            else "conditional_on_unverified_type_extension"
        )
    return event


def _status_get(
    topic: str,
    payload: bytes,
    group_id: str,
    specified=False,
    *,
    root_mac: str | None = None,
    inventory_profile: str = "stock_44",
) -> dict | None:
    if inventory_profile not in STATUS_CHUNK_PROFILES:
        raise ValueError("Unsupported inventory producer profile")
    if specified:
        if len(payload) != 20:
            return None
        records = [(payload[14:20], payload[:14])]
    else:
        if not payload or (len(payload) - 1) % 20:
            return None
        actual_count = (len(payload) - 1) // 20
        # The stock producer repeats the total count in each 44-record chunk.
        # Thus a valid individual chunk can contain fewer than the header count.
        if (
            actual_count > payload[0]
            or actual_count > STATUS_CHUNK_PROFILES[inventory_profile]
            or (not actual_count and payload[0])
        ):
            return None
        records = [
            (payload[o : o + 6], payload[o + 6 : o + 20]) for o in range(1, len(payload), 20)
        ]
    devices = []
    seen = set()
    for mac, data in records:
        peer = mac.hex().upper()
        if peer in seen:
            return None
        seen.add(peer)
        verified_root = root_mac is not None and peer == root_mac
        devices.append(
            {
                "device_id": peer,
                "device_type": data[0],
                "model": MODEL_NAMES.get(data[0], "Unresolved type"),
                "model_confidence": "source_verified_type_enum"
                if data[0] in MODEL_NAMES
                else "structural",
                "status_raw": data[1],
                "unknown_u16_le": int.from_bytes(data[2:4], "little"),
                "flags_raw_hex": data[2:4].hex(),
                "flags_u16_le": int.from_bytes(data[2:4], "little"),
                "verified_root_record": verified_root,
                "measurement_1_hex": data[4:7].hex(),
                "measurement_2_hex": data[7:10].hex(),
                "auxiliary_hex": data[10:14].hex(),
                "normalized_record_hex": (mac + data).hex(),
            }
        )
        devices[-1]["source_profile"] = {
            "basis": "vendor_apk_4.0.21_and_stock_1.1.13",
            "output": interpret_output(data[0], data[1]),
            "feature_flags": {
                "timer_enabled": bool(data[2] & 3) if verified_root else None,
                "normal_timer_running": bool(data[2] & 1) if verified_root else None,
                "delay_active": bool(data[2] & 2) if verified_root else None,
                "green_mode_enabled": bool(data[2] & 4) if verified_root else None,
                "night_mode_enabled": bool(data[2] & 8) if verified_root else None,
                "night_mode_running": bool(data[2] & 8) if verified_root else None,
                "locked": bool(data[2] & 16) if verified_root else None,
                "root_host_bit_raw": bool(data[2] & 32),
                "raw_low_byte": data[2],
                "source_root_context_verified": verified_root,
                "child_flags_known": False,
                "uninterpreted_low_bits": data[2] & 0xC0,
                "uninterpreted_high_byte": data[3],
            },
            "temperature_value": _measurement(data[4:7], bias=32767),
            "temperature_arithmetic_profile": "apk_simple_bias_32767",
            "negative_temperature_profile": "unqualified; historical_C_complements_hundredths",
            "measurement_entities_eligible": False,
            "measurement_units_qualified": False,
            "power_value": _measurement(data[7:10]),
            "temperature_unit": "unverified",
            "power_unit": "unverified",
            "live_firmware_compatibility_verified": False,
        }
    event = _base(topic, payload, "status_inventory")
    event.update(
        group_id=group_id,
        devices=devices,
        count=len(devices),
        observation_source="root_cache_response",
        root_mac=root_mac,
        root_identity_basis="supplied_verified_context" if root_mac else "unknown",
        peer_row_age_available=False,
        peer_health_asserted=False,
        request_nonce_available=False,
        inventory_profile=inventory_profile,
        layout_basis="public_stock_firmware_and_vendor_apk_20_byte_records",
    )
    if not specified:
        event.update(declared_total_count=payload[0], partial_chunk=len(devices) < payload[0])
    return event


def _timer(topic: str, payload: bytes, group_id: str, mode: str) -> dict | None:
    """Legacy timer reports; device-dependent actions and night schedule stay raw."""
    event = _base(topic, payload, "timer_structure")
    event.update(
        group_id=group_id,
        timer_mode=mode,
        layout_basis="vendor_apk_4.0.21",
        live_firmware_compatibility_verified=False,
        observation_source="reported_parameter_response",
        request_nonce_available=False,
        peer_health_asserted=False,
    )
    if mode in {"delay", "greenMode"}:
        expected = 9 if mode == "delay" else 8
        if len(payload) != expected:
            return None
        event["device_id"] = payload[-6:].hex().upper()
        event["fields"] = {
            "duration_seconds": int.from_bytes(payload[:2], "big"),
            "duration_raw_hex": payload[:2].hex(),
            "duration_kind": "remaining_delay"
            if mode == "delay"
            else "configured_inactivity_period",
            "duration_unit_basis": "historical_one_second_counter_loop",
        }
        if mode == "delay":
            event["fields"]["action_raw"] = payload[2]
    elif mode == "nightMode":
        if len(payload) != 12:
            return None
        event["device_id"] = payload[6:].hex().upper()
        start_hour, end_hour = payload[2] & 31, payload[4] & 31
        valid = (
            start_hour < 24
            and end_hour < 24
            and payload[3] < 60
            and payload[5] < 60
            and not (payload[2] & 0xE0 or payload[4] & 0xE0)
        )
        event["fields"] = {
            "schedule_raw_hex": payload[:6].hex(),
            "all_day_flag_raw": payload[0],
            "scheduled_flag_raw": payload[1],
            "all_day_enabled_candidate": bool(payload[0]),
            "scheduled_enabled_candidate": bool(payload[1]),
            "start_hour": start_hour,
            "start_hour_raw": payload[2],
            "start_minute": payload[3],
            "end_hour": end_hour,
            "end_hour_raw": payload[4],
            "end_minute": payload[5],
            "time_valid": valid,
            "crosses_midnight_candidate": (start_hour * 60 + payload[3])
            > (end_hour * 60 + payload[5])
            if valid
            else None,
        }
    elif mode == "normal":
        if len(payload) not in {30, 38}:
            return None
        event["device_id"] = payload[24:30].hex().upper()
        event["fields"] = {"slots": []}
        for offset in range(0, 24, 3):
            days, hour_action, minute_extra = payload[offset : offset + 3]
            hour, minute = hour_action & 31, minute_extra & 63
            event["fields"]["slots"].append(
                {
                    "slot": offset // 3 + 1,
                    "enabled": bool(days & 128),
                    "weekdays": [
                        name
                        for day, name in enumerate(
                            (
                                "Monday",
                                "Tuesday",
                                "Wednesday",
                                "Thursday",
                                "Friday",
                                "Saturday",
                                "Sunday",
                            )
                        )
                        if days & (1 << day)
                    ],
                    "hour": hour,
                    "minute": minute,
                    "minute_raw": minute_extra,
                    "time_valid": hour < 24 and minute_extra < 60,
                    "action_raw": hour_action >> 5,
                    "minute_extra_bits": minute_extra >> 6,
                    "raw_hex": payload[offset : offset + 3].hex(),
                }
            )
        if len(payload) == 38:
            event["fields"]["extension_raw_hex"] = payload[30:].hex()
            event["extension_basis"] = "legacy_kind_81_shape_not_type_identification"
    else:
        return None
    return event


def parse_message(
    topic: str,
    payload: bytes,
    *,
    root_mac: str | None = None,
    qualified_sync: bool = False,
    inventory_profile: str = "stock_44",
) -> dict | None:
    """Return a bounded, generic event or None for unsupported/malformed input.

    Structural/metadata events never imply a current physical power state.
    Retain, receipt time and connection epochs belong to the transport consumer.
    Command topics are classified separately and never converted to feedback.
    """
    if not isinstance(topic, str) or not isinstance(payload, bytes):
        raise TypeError("Expected str topic and bytes payload")
    if root_mac is not None:
        root_mac = _identifier(root_mac, physical=True)
    if not isinstance(qualified_sync, bool):
        raise ValueError("Sync qualification must be explicitly boolean")
    if not topic or any(c in topic for c in "\x00+#") or len(payload) > MAX_PAYLOAD:
        return None
    if topic == "lanbon/a/L8devCompleteDetailReport":
        return _detail(topic, payload)
    if topic == "lanbon/a/L8devElecsumReport":
        return _energy(topic, payload)
    binary = BINARY_TOPIC.fullmatch(topic)
    if binary:
        group_id, suffix = binary["group"].upper(), binary["suffix"]
        if not topic.startswith("lanbon/a/"):
            return _base(topic, payload, "unresolved_direction") | {"group_id": group_id}
        if suffix == "s2m/cmdStatusSynchronous":
            return _status(topic, payload, group_id, qualified_sync=qualified_sync)
        if suffix == "s2m/cmdStatusGet":
            return _status_get(
                topic, payload, group_id, root_mac=root_mac, inventory_profile=inventory_profile
            )
        if suffix == "s2m/cmdStatusGet/specified":
            return _status_get(
                topic,
                payload,
                group_id,
                specified=True,
                root_mac=root_mac,
                inventory_profile=inventory_profile,
            )
        if suffix.startswith("s2m/cmdTimerGet/"):
            return _timer(topic, payload, group_id, suffix.rsplit("/", 1)[1])
        if suffix == "m2s/cmdQuery":
            event = _base(topic, payload, "query") | {"group_id": group_id}
            if len(payload) == 8 and payload[0] in {0x11, 0x12}:
                event["fields"] = {
                    "operation": "all_status" if payload[0] == 0x11 else "specified_status",
                    "target_mac": payload[1:7].hex().upper(),
                    "trailing_byte": payload[7],
                    "layout_basis": "vendor_apk_4.0.21_constructor",
                    "source_profile_read_only": payload[0] == 0x11,
                    "peer_freshness_proven": False,
                    "potential_side_effects": [] if payload[0] == 0x11 else ["beep_or_ui_notice"],
                }
            return event
        if suffix == "cmdControl":
            event = _base(topic, payload, "command") | {"group_id": group_id}
            if len(payload) == 7:
                event["fields"] = {
                    "command_raw": payload[0],
                    "target_mac": payload[1:].hex().upper(),
                }
            return event
        return _base(topic, payload, "unresolved_binary") | {"group_id": group_id}
    text = TEXT_TOPIC.fullmatch(topic)
    if not text:
        return None
    suffix = text["suffix"]
    if suffix not in {"state", "brightnessState", "set", "brightnessSet"}:
        return None
    channel = int(text["channel"]) if text["channel"] else None
    if channel is not None and not 1 <= channel <= 4:
        return None
    if suffix in {"set", "brightnessSet"}:
        event = _base(topic, payload, "command", "observed")
    elif suffix == "state" and payload in {b"ON", b"OFF"}:
        event = _base(topic, payload, "power", "observed")
        event["fields"] = {"power": payload.decode("ascii")}
    elif (
        suffix == "brightnessState"
        and text["domain"] == "light"
        and len(payload) <= 32
        and PERCENT.fullmatch(payload)
    ):
        percent = float(payload)
        if not 0 <= percent <= 100:
            return None
        event = _base(topic, payload, "brightness", "observed")
        event["fields"] = {"brightness": percent}
    else:
        return None
    return event | {
        "group_id": text["group"].upper(),
        "device_id": text["device"].upper(),
        "channel": channel,
        "topic_domain": text["domain"],
        "discovery_prefix": text["prefix"],
        "native_base": topic.rsplit("/", 1)[0],
        "native_route_source": "feedback"
        if event["kind"] in {"power", "brightness"}
        else "command",
    }
