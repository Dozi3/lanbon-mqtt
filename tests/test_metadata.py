"""Synthetic source-profile metadata contracts; no device or network access."""

import copy
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "portable_lanbon_metadata",
    Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/metadata.py",
)
metadata = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metadata)

ROOT, CHILD, GROUP, IP = "020000000001", "080000000002", "102030405060", "10.0.0.10"


def envelopes():
    mesh = {
        "http_status": 200,
        "request_host": IP,
        "headers": {"Mesh-Node-Num": "2", "Mesh-Node-Mac": ROOT + ", 80000000002"},
        "body": {"status_code": 0},
    }
    device = {
        "http_status": 200,
        "request_host": IP,
        "headers": {"Mesh-Node-Mac": ROOT},
        "body": {
            "status_code": 0,
            "status_msg": "MDF_OK",
            "self_mac": ROOT,
            "parent_mac": "020000000099",
            "mesh_id": GROUP,
            "layer": 1,
            "rssi": -51,
            "name": "light_0001",
            "version": "1.1.0",
            "idf_version": "v3.3.5-dirty",
            "mdf_version": "",
            "mlink_version": 2,
            "mlink_trigger": 0,
            "tsf_time": "11535320193288",
            "tid": "1",
            "characteristics": [
                {
                    "cid": 0,
                    "name": "on",
                    "format": "int",
                    "perms": 7,
                    "value": 0,
                    "min": 0,
                    "max": 3,
                    "step": 1,
                }
            ],
        },
    }
    return mesh, device


def parse(mesh=None, device=None, **kwargs):
    default_mesh, default_device = envelopes()
    return metadata.parse_metadata(
        mesh or default_mesh,
        device or default_device,
        root_ip=kwargs.pop("root_ip", IP),
        expected_root_mac=ROOT,
        expected_groups=kwargs.pop("expected_groups", [GROUP]),
        **kwargs,
    )


def test_verified_root_metadata_is_separate_from_lanbon_output_and_firmware():
    event = parse(observed_at=datetime(2026, 10, 15, tzinfo=UTC))
    assert event["device_id"] == ROOT and event["group_id"] == GROUP
    assert event["source"] == "read_only_http_metadata"
    fields = event["fields"]
    assert fields["mesh_members"] == [ROOT, CHILD]
    assert fields["rssi"] == -51 and not fields["rssi_fallback"]
    assert fields["version"] == "1.1.0"
    assert fields["version_namespace"] == "mlink_application_not_lanbon_firmware"
    assert fields["tsf_time_raw"] == "11535320193288"
    assert fields["characteristics"][0]["value"] == 0
    assert not fields["characteristic_state_qualified"]
    assert "reported_power" not in fields and "device_type" not in fields
    assert event["observed_at"] == "2026-10-15T00:00:00+00:00"


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "0.0.0.0",
        "8.8.8.8",
        "100.64.0.1",
        "http://10.0.0.10",
        "10.0.0.10:80",
        "10.0.0.10/path",
        "user@10.0.0.10",
        "::1",
        "fd00::1",
        " 10.0.0.10",
    ],
)
def test_only_qualified_private_ipv4_literals_are_accepted(ip):
    with pytest.raises(ValueError):
        parse(root_ip=ip)


@pytest.mark.parametrize(
    "key,value",
    [
        ("self_mac", CHILD),
        ("mesh_id", "AABBCCDDEEFF"),
        ("layer", 2),
        ("layer", True),
        ("status_code", 1),
        ("status_code", False),
        ("rssi", -121),
        ("rssi", float("nan")),
        ("parent_mac", "010000000001"),
        ("tsf_time", "1e12"),
        ("tid", "x\n"),
    ],
)
def test_bad_identity_profile_status_or_unbounded_values_rejected(key, value):
    mesh, device = envelopes()
    device["body"][key] = value
    with pytest.raises(ValueError):
        parse(mesh, device)


@pytest.mark.parametrize(
    "which,key,value",
    [
        ("mesh", "Mesh-Node-Num", "3"),
        ("mesh", "Mesh-Node-Mac", ROOT + "," + ROOT),
        ("mesh", "Mesh-Node-Mac", "1"),
        ("device", "Mesh-Node-Mac", CHILD),
    ],
)
def test_header_membership_or_target_mismatch_rejected(which, key, value):
    mesh, device = envelopes()
    (mesh if which == "mesh" else device)["headers"][key] = value
    with pytest.raises(ValueError):
        parse(mesh, device)


def test_actual_request_host_and_http_failure_are_not_metadata_success():
    mesh, device = envelopes()
    device["request_host"] = "10.0.0.11"
    with pytest.raises(ValueError):
        parse(mesh, device)
    device = envelopes()[1]
    device["http_status"] = 302
    with pytest.raises(ValueError):
        parse(mesh, device)


def test_rssi_sdk_fallback_is_unknown_not_measured_weak_signal():
    mesh, device = envelopes()
    device["body"]["rssi"] = -120
    fields = parse(mesh, device)["fields"]
    assert fields["rssi_raw"] == -120
    assert fields["rssi"] is None and fields["rssi_fallback"]


def test_characteristics_are_bounded_namespaced_and_unknown_keys_are_not_copied():
    mesh, device = envelopes()
    device["body"]["password"] = "do-not-export"
    device["body"]["characteristics"][0]["unknown"] = {"token": "do-not-export"}
    fields = parse(mesh, device)["fields"]
    assert "password" not in fields
    assert "unknown" not in fields["characteristics"][0]
    device["body"]["characteristics"] *= 33
    with pytest.raises(ValueError):
        parse(mesh, device)


def test_malformed_mac_separators_and_oversized_numbers_are_rejected():
    mesh, device = envelopes()
    device["body"]["self_mac"] = "0:2:0000000001"
    with pytest.raises(ValueError):
        parse(mesh, device)
    device = envelopes()[1]
    device["body"]["characteristics"][0]["value"] = 1 << 1000
    with pytest.raises(ValueError):
        parse(mesh, device)


def test_group_iterator_and_http_json_are_bounded():
    def never_ending_groups():
        while True:
            yield GROUP

    with pytest.raises(ValueError):
        parse(expected_groups=never_ending_groups())
    mesh, device = envelopes()
    device["body"]["ignored_blob"] = "x" * 16384
    with pytest.raises(ValueError):
        parse(mesh, device)


def test_no_mutation_and_timezone_receipt_conversion():
    mesh, device = envelopes()
    before = copy.deepcopy((mesh, device))
    event = parse(mesh, device, observed_at="2026-10-15T02:00:00+01:00")
    assert (mesh, device) == before
    assert event["observed_at"] == "2026-10-15T01:00:00+00:00"
    with pytest.raises(ValueError):
        parse(observed_at="2026-10-15T01:00:00")


def test_udp_plaintext_tick_and_output_remain_raw_not_utc_or_state():
    payload = f"mac={ROOT}\r\nflag=-1\r\ntype=status[{CHILD}_73]\r\n".encode()
    event = metadata.parse_udp_notice(payload, expected_root_mac=ROOT)
    assert event["device_id"] == CHILD and event["root_mac"] == ROOT
    assert event["fields"]["output_raw"] == 73
    assert event["fields"]["flag_raw"] == "-1"
    assert event["fields"]["flag_u32"] == 0xFFFFFFFF
    assert not event["authenticated"] and not event["physical_state_qualified"]
    assert "group_id" not in event and "reported_power" not in event["fields"]
    assert metadata.parse_udp_notice(payload, expected_root_mac=CHILD) is None


def child_envelope():
    child = copy.deepcopy(envelopes()[1])
    child["headers"]["Mesh-Node-Mac"] = CHILD
    child["body"].update(self_mac=CHILD, parent_mac=ROOT, layer=2, rssi=-68)
    return child


def parse_child(child=None, *, mesh=None, root_info=None, expected_peer=CHILD):
    default_mesh, default_root = envelopes()
    return metadata.parse_child_metadata(
        mesh or default_mesh,
        root_info or default_root,
        child or child_envelope(),
        root_ip=IP,
        expected_root_mac=ROOT,
        expected_peer_mac=expected_peer,
        expected_groups=[GROUP],
        observed_at="2026-10-15T08:00:00+00:00",
    )


def test_child_uses_gateway_address_without_claiming_it_as_child_ip():
    event = parse_child()
    assert event["device_id"] == CHILD and event["root_mac"] == ROOT
    assert event["role"] == "child" and event["group_id"] == GROUP
    assert event["fields"]["gateway_address"] == IP
    assert "ip_address" not in event["fields"]
    assert event["fields"]["layer"] == 2 and event["fields"]["rssi"] == -68
    assert event["fields"]["parent_mac"] == ROOT
    assert not event["fields"]["characteristic_state_qualified"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("self_mac", ROOT),
        ("mesh_id", "AABBCCDDEEFF"),
        ("layer", 1),
        ("layer", 26),
        ("layer", True),
        ("status_code", 1),
        ("rssi", -121),
    ],
)
def test_child_bad_identity_group_layer_or_profile_rejected(key, value):
    child = child_envelope()
    child["body"][key] = value
    with pytest.raises(ValueError):
        parse_child(child)


def test_child_requires_successful_root_proof_and_current_membership():
    root = envelopes()[1]
    root["body"]["self_mac"] = CHILD
    with pytest.raises(ValueError):
        parse_child(root_info=root)
    with pytest.raises(ValueError):
        parse_child(expected_peer="020000000003")
    with pytest.raises(ValueError):
        parse_child(expected_peer=ROOT)
    child = child_envelope()
    child["headers"]["Mesh-Node-Mac"] = ROOT
    with pytest.raises(ValueError):
        parse_child(child)


def test_child_target_host_and_characteristic_caps_share_root_guards():
    child = child_envelope()
    child["request_host"] = "10.0.0.11"
    with pytest.raises(ValueError):
        parse_child(child)
    child = child_envelope()
    child["body"]["characteristics"] *= 33
    with pytest.raises(ValueError):
        parse_child(child)


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 129,
        b"mac=x\r\nflag=1\r\ntype=status[x_0]\r\n",
        f"mac={ROOT}\r\nflag=4294967296\r\ntype=status[{CHILD}_0]\r\n".encode(),
        f"mac={ROOT}\r\nflag=1\r\ntype=status[{CHILD}_256]\r\n".encode(),
    ],
)
def test_udp_invalid_or_unbounded_envelopes_are_ignored(payload):
    assert metadata.parse_udp_notice(payload) is None
