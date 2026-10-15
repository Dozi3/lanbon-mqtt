"""Portable protocol contracts. These tests never connect or send MQTT packets."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "portable_lanbon_protocol",
    Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/protocol.py",
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)


@pytest.mark.parametrize("raw,text", [(30048, "3.0.48"), (30120, "3.1.20"), (10113, "1.1.13")])
def test_firmware_numeric_encoding_confirmed_by_serializer(raw, text):
    assert protocol.version_string(raw) == text


def test_detail_extracts_version_from_mac_records():
    payload = bytes.fromhex("01080200000000000000000000000000aabbccddeeff75600123456789ab75a8")
    event = protocol.parse_message("lanbon/a/L8devCompleteDetailReport", payload)
    assert event["count"] == 2
    assert event["devices"][0]["device_id"] == "AABBCCDDEEFF"
    assert event["devices"][0]["firmware_version"] == "3.0.48"
    assert event["devices"][1]["firmware_version"] == "3.1.20"
    assert "group_id" not in event  # Global inventory alone cannot identify the configured mesh.
    assert "power" not in event["fields"]


@pytest.mark.parametrize(
    "group,mac,prefix",
    [
        ("112233445566", "AABBCCDDEEFF", "homeassistant"),
        ("FFEEDDCCBBAA", "0123456789AB", "custom/discovery"),
    ],
)
def test_plaintext_is_portable_across_home_and_prefix(group, mac, prefix):
    power = protocol.parse_message(f"{prefix}/{group}/light/{mac}/state", b"OFF")
    brightness = protocol.parse_message(f"{prefix}/{group}/light/{mac}/brightnessState", b"50")
    assert power["device_id"] == brightness["device_id"] == mac
    assert power["group_id"] == brightness["group_id"] == group
    assert power["fields"] == {"power": "OFF"}
    assert brightness["fields"] == {"brightness": 50}
    assert brightness["discovery_prefix"] == prefix


def test_relay_channel_identity_stays_independent():
    first = protocol.parse_message("homeassistant/112233445566/switch/AABBCCDDEEFF-01/state", b"ON")
    second = protocol.parse_message(
        "homeassistant/112233445566/switch/AABBCCDDEEFF-02/state", b"OFF"
    )
    assert first["device_id"] == second["device_id"]
    assert first["channel"] == 1 and second["channel"] == 2
    assert first["fields"]["power"] == "ON" and second["fields"]["power"] == "OFF"


@pytest.mark.parametrize("suffix,payload", [("set", b"ON"), ("brightnessSet", b"90")])
def test_command_topics_are_not_reports(suffix, payload):
    event = protocol.parse_message(
        f"homeassistant/112233445566/light/AABBCCDDEEFF/{suffix}", payload
    )
    assert event["kind"] == "command"
    assert event["fields"] == {}


@pytest.mark.parametrize(
    "hex_data,type_code",
    [
        ("3200000000aabbccddeeff", None),
        ("3200000000aabbccddeeff4d", 77),
    ],
)
def test_legacy_and_current_sync_preserve_layout_without_power_inference(hex_data, type_code):
    event = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdStatusSynchronous", bytes.fromhex(hex_data)
    )
    assert event["fields"]["output_code"] == 50
    assert event["fields"]["candidate_device_type"] == type_code
    assert event["device_id"] == "AABBCCDDEEFF"
    assert event["confidence"] == "structural"
    assert "power" not in event["fields"]
    assert "brightness" not in event["fields"]


def test_complete_status_inventory_and_specified_have_same_structural_fields():
    mac = bytes.fromhex("aabbccddeeff")
    data = bytes.fromhex("4d32020000008034120000000000")
    all_devices = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdStatusGet", b"\x01" + mac + data
    )
    specified = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdStatusGet/specified", data + mac
    )
    assert all_devices["devices"] == specified["devices"]
    assert all_devices["devices"][0]["status_raw"] == 50
    assert all_devices["devices"][0]["device_type"] == 77


def test_energy_unknown_units_are_explicit():
    event = protocol.parse_message(
        "lanbon/a/L8devElecsumReport", bytes.fromhex("010100112233445566aabbccddeeff010203")
    )
    assert event["group_id"] == "112233445566"
    assert event["devices"][0]["measurement_hex"] == "010203"
    assert event["devices"][0]["units"] == "unverified"


def test_status_inventory_accepts_partial_chunks_with_repeated_total_count():
    data = bytes.fromhex("4d320200800019000c3200000000")
    records = b"".join(bytes.fromhex(f"02000000{i:04x}") + data for i in range(44))
    event = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdStatusGet",
        bytes([100]) + records,
        root_mac="020000000000",
    )
    assert event["count"] == 44
    assert event["declared_total_count"] == 100
    assert event["partial_chunk"] is True
    assert event["devices"][0]["source_profile"]["temperature_value"] == 1.25
    assert event["devices"][0]["source_profile"]["power_value"] == 12.5
    assert event["devices"][0]["source_profile"]["feature_flags"]["timer_enabled"]
    assert not event["devices"][0]["source_profile"]["live_firmware_compatibility_verified"]


@pytest.mark.parametrize("device_type,count", [(74, 1), (75, 2), (76, 3)])
def test_relay_state_bits_and_changed_bits_are_independent(device_type, count):
    for raw in range(256):
        profile = protocol.interpret_output(device_type, raw)
        assert len(profile["channels"]) == count
        for channel in profile["channels"]:
            index = channel["channel"] - 1
            assert (channel["reported_power"] == "ON") == bool(raw & (1 << index))
            assert channel["changed_flag_raw"] == bool(raw & (1 << (index + 5)))
            assert channel["changed_flag_qualified"] is False
        assert not profile["physical_state_verified"]


@pytest.mark.parametrize("device_type", [4, 60, 66, 77])
def test_dimmer_uses_low_seven_bits_without_clamping_out_of_percent_range(device_type):
    profile = protocol.interpret_output(device_type, 0x80, context="sync", qualified=True)
    assert profile["reported_power"] == "OFF"
    if device_type == 77:
        assert profile["output_percent"] == 0
    else:
        assert "output_percent" not in profile
    assert profile["high_bit"] is True
    profile = protocol.interpret_output(device_type, 255, context="sync", qualified=True)
    assert profile["output_level"] == 127
    assert "output_percent" not in profile


def test_single_gang_profile_preserves_unused_channel_bits_as_unknown():
    assert protocol.interpret_output(74, 0x42)["uninterpreted_bits"] == 0x42


def test_unknown_type_and_invalid_decimal_fraction_stay_uninterpreted():
    assert protocol.interpret_output(255, 127)["interpretation"] == "unsupported_device_type"
    data = bytes.fromhex("4d001f7f8000ff00008000000000")
    event = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdStatusGet/specified",
        data + bytes.fromhex("aabbccddeeff"),
    )
    row = event["devices"][0]
    assert row["measurement_1_hex"] == "8000ff"
    assert row["source_profile"]["temperature_value"] is None
    assert row["source_profile"]["power_value"] is None
    assert row["source_profile"]["feature_flags"]["uninterpreted_high_byte"] == 127


@pytest.mark.parametrize(
    "mode,hex_data,seconds",
    [
        ("delay", "025801aabbccddeeff", 600),
        ("greenMode", "00ffaabbccddeeff", 255),
    ],
)
def test_timer_countdowns_preserve_duration_and_raw_action(mode, hex_data, seconds):
    event = protocol.parse_message(
        f"lanbon/a/MAC:112233445566/s2m/cmdTimerGet/{mode}", bytes.fromhex(hex_data)
    )
    assert event["device_id"] == "AABBCCDDEEFF"
    assert event["fields"]["duration_seconds"] == seconds
    assert event["kind"] == "timer_structure"
    assert "reported_power" not in event["fields"]
    if mode == "delay":
        assert event["fields"]["action_raw"] == 1


def test_normal_timer_preserves_invalid_time_and_device_specific_action():
    # Enabled Monday+Sunday, hour 9, action bits 3, minute 30, extra bits 2.
    slots = bytes.fromhex("c1699e") + bytes.fromhex("ff1f3f") + bytes(18)
    payload = slots + bytes.fromhex("aabbccddeeff")
    event = protocol.parse_message("lanbon/a/MAC:112233445566/s2m/cmdTimerGet/normal", payload)
    first, second = event["fields"]["slots"][:2]
    assert first["weekdays"] == ["Monday", "Sunday"]
    assert first["enabled"] and not first["time_valid"]
    assert first["minute_raw"] == 158  # Historical C minute is full-byte; high bits unqualified.
    assert first["hour"] == 9 and first["minute"] == 30
    assert first["action_raw"] == 3 and first["minute_extra_bits"] == 2
    assert not second["time_valid"]
    assert second["hour"] == 31 and second["minute"] == 63
    extended = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdTimerGet/normal",
        payload + bytes.fromhex("0001020304050607"),
    )
    assert extended["fields"]["extension_raw_hex"] == "0001020304050607"
    assert "device_type" not in extended


def test_night_mode_is_not_misread_as_normal_timer_slots():
    event = protocol.parse_message(
        "lanbon/a/MAC:112233445566/s2m/cmdTimerGet/nightMode",
        bytes.fromhex("010203040506aabbccddeeff"),
    )
    assert event["fields"]["schedule_raw_hex"] == "010203040506"
    assert event["fields"]["all_day_flag_raw"] == 1
    assert event["fields"]["scheduled_flag_raw"] == 2
    assert "reported_power" not in event["fields"]
    assert event["device_id"] == "AABBCCDDEEFF"


def test_binary_commands_and_queries_cannot_become_reported_state():
    control = protocol.parse_message(
        "lanbon/a/MAC:112233445566/cmdControl", bytes.fromhex("01aabbccddeeff")
    )
    query = protocol.parse_message(
        "lanbon/a/MAC:112233445566/m2s/cmdQuery", bytes.fromhex("12aabbccddeeff00")
    )
    assert control["kind"] == "command"
    assert control["fields"]["command_raw"] == 1
    assert query["kind"] == "query"
    assert query["fields"]["operation"] == "specified_status"
    assert "power" not in control["fields"] and "power" not in query["fields"]


def test_unknown_or_truncated_timer_shape_is_rejected():
    for mode, payload in [("delay", bytes(8)), ("normal", bytes(31)), ("future", bytes(30))]:
        assert (
            protocol.parse_message(f"lanbon/a/MAC:112233445566/s2m/cmdTimerGet/{mode}", payload)
            is None
        )


@pytest.mark.parametrize(
    "topic,payload",
    [
        ("lanbon/a/L8devCompleteDetailReport", b"\x01\x08\x01"),
        ("lanbon/a/MAC:112233445566/s2m/cmdStatusGet", b"\x01" + b"\x00" * 40),
        ("lanbon/a/MAC:112233445566/s2m/cmdStatusSynchronous", b"\x00" * 10),
        ("homeassistant/112233445566/light/AABBCCDDEEFF/state", b" on "),
        ("homeassistant/112233445566/light/AABBCCDDEEFF/brightnessState", b"nan"),
        ("homeassistant/112233445566/light/AABBCCDDEEFF/brightnessState", b"101"),
        ("homeassistant/112233445566/switch/AABBCCDDEEFF-99/state", b"ON"),
    ],
)
def test_malformed_or_unvalidated_values_are_rejected(topic, payload):
    assert protocol.parse_message(topic, payload) is None


def test_other_topics_are_uninterpreted_not_current_state():
    assert protocol.parse_message("unrelated/device/state", b"ON") is None
    event = protocol.parse_message(
        "lanbon/b/MAC:112233445566/s2m/cmdStatusSynchronous",
        bytes.fromhex("3200000000aabbccddeeff4d"),
    )
    assert event["kind"] == "unresolved_direction"
    assert not event["fields"]
