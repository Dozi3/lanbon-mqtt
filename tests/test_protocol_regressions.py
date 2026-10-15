"""Source-based protocol regression and pure control-message contracts."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "candidate_protocol_regressions",
    Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/protocol.py",
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)
GROUP, ROOT, PEER = "0200000000F0", "020000000001", "020000000002"
TOPIC = f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet"


def record(peer=PEER, *, kind=77, output=25, flags=0, temp="7fff00"):
    return (
        bytes.fromhex(peer)
        + bytes([kind, output])
        + flags.to_bytes(2, "little")
        + bytes.fromhex(temp)
        + bytes(7)
    )


@pytest.mark.parametrize("triplet,expected", [("80232f", 36.47), ("7fff00", 0), ("7ffc18", -2.76)])
def test_source_apk_bias_and_negative_qualification(triplet, expected):
    row = protocol.parse_message(TOPIC, b"\x01" + record(temp=triplet))["devices"][0]
    profile = row["source_profile"]
    assert profile["temperature_value"] == pytest.approx(expected, rel=0, abs=1e-8)
    assert profile["temperature_arithmetic_profile"] == "apk_simple_bias_32767"
    assert "unqualified" in profile["negative_temperature_profile"]
    assert profile["measurement_entities_eligible"] is False
    assert profile["temperature_unit"] == profile["power_unit"] == "unverified"


def test_contradictory_duplicate_status_inventory_rejected_before_observation():
    assert protocol.parse_message(TOPIC, b"\x02" + record(output=0) + record(output=100)) is None


def test_duplicate_global_metadata_and_energy_inventories_rejected():
    detail = b"\x01\x08\x02" + bytes(13) + (bytes.fromhex(PEER) + b"\x75\x60") * 2
    assert protocol.parse_message("lanbon/a/L8devCompleteDetailReport", detail) is None
    energy = b"\x02\x01\x00" + bytes.fromhex(GROUP) + (bytes.fromhex(PEER) + bytes(3)) * 2
    assert protocol.parse_message("lanbon/a/L8devElecsumReport", energy) is None


def test_stock_chunk_bound_has_unique_records_and_repeated_total_semantics():
    records = [record(f"02000000{i:04X}") for i in range(1, 46)]
    accepted = protocol.parse_message(TOPIC, bytes([100]) + b"".join(records[:44]))
    assert accepted["count"] == 44 and accepted["declared_total_count"] == 100
    assert accepted["partial_chunk"] is True and accepted["inventory_profile"] == "stock_44"
    assert protocol.parse_message(TOPIC, bytes([100]) + b"".join(records)) is None
    with pytest.raises(ValueError, match="producer profile"):
        protocol.parse_message(TOPIC, b"\x01" + records[0], inventory_profile="unknown")


def test_inventory_dimmer_full_byte_and_sync_explicit_profile_are_distinct():
    for raw in range(256):
        inventory = protocol.interpret_output(77, raw)
        assert inventory["raw"] == raw and inventory["high_bit"] == bool(raw & 128)
        if raw <= 100:
            assert inventory["output_percent"] == raw
        else:
            assert "output_percent" not in inventory and "reported_power" not in inventory
        sync = protocol.interpret_output(77, raw, context="sync")
        assert "output_percent" not in sync and "reported_power" not in sync
        selected = protocol.interpret_output(77, raw, context="sync", qualified=True)
        if raw & 127 <= 100:
            assert selected["output_percent"] == raw & 127
        else:
            assert "output_percent" not in selected


def test_sync_qualification_cannot_be_enabled_by_truthy_non_boolean_inputs():
    for invalid in ("false", 1, None):
        with pytest.raises(ValueError, match="explicitly boolean"):
            protocol.interpret_output(77, 228, context="sync", qualified=invalid)


@pytest.mark.parametrize("kind", [74, 75, 76])
def test_relay_sync_does_not_overwrite_other_gangs_from_unqualified_action_masks(kind):
    for qualified in (False, True):
        for raw in range(256):
            result = protocol.interpret_output(kind, raw, context="sync", qualified=qualified)
            assert "channels" not in result and "reported_power" not in result
    inventory = protocol.interpret_output(kind, 1)
    assert inventory["channels"][0]["reported_power"] == "ON"
    assert all(row["changed_flag_qualified"] is False for row in inventory["channels"])


def test_root_context_is_not_position_group_or_child_feature_proof():
    payload = b"\x02" + record(PEER, flags=0) + record(ROOT, flags=32)
    parsed = protocol.parse_message(TOPIC, payload, root_mac=ROOT)
    assert parsed["group_id"] == GROUP and parsed["root_mac"] == ROOT
    assert parsed["observation_source"] == "root_cache_response"
    assert parsed["peer_row_age_available"] is parsed["peer_health_asserted"] is False
    assert parsed["request_nonce_available"] is False
    child, root = parsed["devices"]
    assert child["verified_root_record"] is False and root["verified_root_record"] is True
    assert child["flags_raw_hex"] == "0000" and root["flags_u16_le"] == 32
    assert child["source_profile"]["feature_flags"]["locked"] is None
    assert root["source_profile"]["feature_flags"]["root_host_bit_raw"] is True
    unknown = protocol.parse_message(TOPIC, payload)
    assert all(not row["verified_root_record"] for row in unknown["devices"])


@pytest.mark.parametrize("timezone", [b"\x01\x00", b"\x00\x00", b"\x05\x1e"])
def test_global_energy_header_is_mesh_group_not_root_and_timezone_is_raw(timezone):
    payload = b"\x01" + timezone + bytes.fromhex(GROUP + PEER) + bytes(3)
    event = protocol.parse_message("lanbon/a/L8devElecsumReport", payload)
    assert event["group_id"] == GROUP and event["group_id_basis"] == "payload_bssid_or_mesh_id"
    assert event["root_mac"] is None and event["root_mac_available"] is False
    assert event["timezone_raw_hex"] == timezone.hex()
    assert event["measurement_entities_eligible"] is event["peer_health_asserted"] is False


@pytest.mark.parametrize("kind,count", [(74, 1), (75, 2), (76, 3), (77, 1)])
def test_control_profile_is_bounded_and_not_a_delivery_claim(kind, count):
    profile = protocol.control_profile(kind)
    assert profile["channels"] == count and profile["brightness"] is (kind == 77)
    assert profile["physical_delivery_verified"] is False
    assert profile["requires_observed_native_route"] is (kind != 77)


@pytest.mark.parametrize("kind", [True, None, "77", 77.0, 0, 4, 66, 255])
def test_other_or_untyped_control_profiles_are_not_enabled(kind):
    assert protocol.control_profile(kind) is None


@pytest.mark.parametrize("level", [0, 1, 25, 57, 100])
def test_dimmer_messages_use_native_percent_topics_without_publishing(level):
    assert protocol.native_control_message(GROUP, PEER, 77, "brightness", level) == (
        f"homeassistant/{GROUP}/light/{PEER}/brightnessSet",
        str(level).encode(),
    )
    assert protocol.native_control_message(GROUP, PEER, 77, "power", "OFF") == (
        f"homeassistant/{GROUP}/light/{PEER}/set",
        b"OFF",
    )


@pytest.mark.parametrize("kind,count", [(74, 1), (75, 2), (76, 3)])
def test_relay_routes_require_observed_matching_native_channel(kind, count):
    for channel in range(1, count + 1):
        base = f"homeassistant/{GROUP}/switch/{PEER}-{channel:02}"
        assert protocol.native_control_message(
            GROUP,
            PEER,
            kind,
            "power",
            "ON",
            channel,
            observed_native_base=base,
        ) == (base + "/set", b"ON")
    with pytest.raises(ValueError, match="observed native route"):
        protocol.native_control_message(GROUP, PEER, kind, "power", "ON", 1)


def test_single_relay_factory_light_route_is_preserved_instead_of_guessed():
    base = f"homeassistant/{GROUP}/light/{PEER}"
    assert protocol.native_control_message(
        GROUP, PEER, 74, "power", "OFF", 1, observed_native_base=base, component="light"
    ) == (base + "/set", b"OFF")
    feedback = protocol.parse_message(base + "/state", b"OFF")
    command = protocol.parse_message(base + "/set", b"OFF")
    assert feedback["native_base"] == base and feedback["native_route_source"] == "feedback"
    assert command["native_route_source"] == "command"


@pytest.mark.parametrize("value", [-1, 101, True, 25.5, "25", None])
def test_invalid_dimmer_values_cannot_be_serialized(value):
    with pytest.raises(ValueError):
        protocol.native_control_message(GROUP, PEER, 77, "brightness", value)


@pytest.mark.parametrize(
    "base,channel",
    [
        (f"homeassistant/0200000000F1/switch/{PEER}-01", 1),
        (f"homeassistant/{GROUP}/switch/020000000003-01", 1),
        (f"homeassistant/{GROUP}/switch/{PEER}-02", 1),
        (f"homeassistant/{GROUP}/switch/{PEER}", 1),
        (f"homeassistant/{GROUP}/switch/{PEER}-99", 1),
        (f"other/{GROUP}/switch/{PEER}-01", 1),
    ],
)
def test_multi_relay_route_scope_and_suffix_are_enforced(base, channel):
    with pytest.raises(ValueError):
        protocol.native_control_message(
            GROUP, PEER, 75, "power", "ON", channel, observed_native_base=base
        )


@pytest.mark.parametrize("peer", ["000000000000", "FFFFFFFFFFFF", "030000000001"])
def test_invalid_physical_control_target_cannot_be_serialized(peer):
    with pytest.raises(ValueError):
        protocol.native_control_message(GROUP, peer, 77, "power", "ON")


def test_unsafe_query_opcode_is_labelled_not_a_read_only_grant():
    event = protocol.parse_message(
        f"lanbon/a/MAC:{GROUP}/m2s/cmdQuery", b"\x12" + bytes.fromhex(PEER) + b"\x00"
    )
    assert event["fields"]["source_profile_read_only"] is False
    assert event["fields"]["potential_side_effects"] == ["beep_or_ui_notice"]
