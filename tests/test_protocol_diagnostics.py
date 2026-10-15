"""Independent pinned-source field-preservation and qualification regressions."""

import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "diagnostic_lanbon_protocol",
    Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/protocol.py",
)
protocol = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(protocol)
PEER, GROUP = "020000000002", "102030405060"


@pytest.mark.parametrize("minute", [0, 59, 60, 128, 158, 255])
def test_normal_timer_full_minute_byte_cannot_be_masked_into_valid_clock(minute):
    payload = bytes([0xC1, 9 | 3 << 5, minute]) + bytes(21) + bytes.fromhex(PEER)
    event = protocol.parse_message(f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/normal", payload)
    slot = event["fields"]["slots"][0]
    assert slot["minute_raw"] == minute
    assert slot["minute"] == minute & 63
    assert slot["time_valid"] is (minute < 60)
    assert event["observation_source"] == "reported_parameter_response"
    assert not event["request_nonce_available"]


def test_night_report_retains_configuration_separate_from_current_running_state():
    # Synthetic schedule and peer preserve the qualified layout and disabled flags.
    event = protocol.parse_message(
        f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/nightMode",
        bytes.fromhex("0000150b0611" + PEER),
    )
    fields = event["fields"]
    assert fields["schedule_raw_hex"] == "0000150b0611"
    assert not fields["all_day_enabled_candidate"] and not fields["scheduled_enabled_candidate"]
    assert (
        fields["start_hour"],
        fields["start_minute"],
        fields["end_hour"],
        fields["end_minute"],
    ) == (21, 11, 6, 17)
    assert fields["time_valid"] and fields["crosses_midnight_candidate"]
    assert "reported_power" not in fields and "night_mode_running" not in fields


def test_energy_candidate_is_unqualified_hour_window_not_total_increasing():
    payload = b"\x01\x01\x00" + bytes.fromhex(GROUP + PEER + "000132")
    event = protocol.parse_message("lanbon/a/L8devElecsumReport", payload)
    row = event["devices"][0]
    assert row["measurement_candidate"] == 1.5
    assert row["unit"] is None
    assert row["measurement_entities_eligible"] is False
    assert row["historical_counter_profile"] == "hour_window_not_cumulative_local_record"
    assert row["counter_reset_qualified"] is False
    assert "state_class" not in row


def test_normal_and_delay_flags_are_distinct_only_for_verified_root():
    # Kind4D/output25/flagsbit1 only; triplets/aux remain unqualified.
    record = bytes.fromhex(PEER + "4d190200" + "7fff00" + "000000" + "01640000")
    topic = f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet"
    child = protocol.parse_message(topic, b"\x01" + record)["devices"][0]
    root = protocol.parse_message(topic, b"\x01" + record, root_mac=PEER)["devices"][0]
    assert child["source_profile"]["feature_flags"]["delay_active"] is None
    flags = root["source_profile"]["feature_flags"]
    assert flags["timer_enabled"] and flags["delay_active"]
    assert not flags["normal_timer_running"]
    assert root["source_profile"]["measurement_units_qualified"] is False


@pytest.mark.parametrize("kind", [0x4A, 0x4B, 0x4C, 0x4D])
def test_parameter_getter_builder_is_fixed_to_four_read_only_requests(kind):
    messages = protocol.parameter_getter_messages(GROUP, PEER, kind)
    assert len(messages) == 4
    assert [topic for topic, _ in messages] == [f"lanbon/a/MAC:{GROUP}/m2s/cmdQuery"] * 4
    assert [payload.hex() for _, payload in messages] == [
        opcode + PEER.lower() + "00" for opcode in ("a0", "a1", "a2", "a3")
    ]
    assert all(len(payload) == 8 and payload[0] != 0x12 for _, payload in messages)


@pytest.mark.parametrize(
    "group,peer,kind",
    [
        (GROUP, PEER, 0x4E),
        (GROUP, PEER, True),
        (GROUP, "FFFFFFFFFFFF", 0x4D),
        (GROUP, "010000000001", 0x4D),
        ("000000000000", PEER, 0x4D),
        (GROUP + "/set", PEER, 0x4D),
    ],
)
def test_parameter_getter_builder_rejects_unsupported_targets(group, peer, kind):
    with pytest.raises(ValueError):
        protocol.parameter_getter_messages(group, peer, kind)
