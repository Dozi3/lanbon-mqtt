"""Passive discovery contracts, with synthetic IDs and independent wire fixtures."""

import json

import pytest

from custom_components.lanbon_mqtt.discovery import (
    ENERGY_TOPIC,
    MAX_PEERS_PER_GROUP,
    DiscoveryCollector,
    native_config_hint,
    normalize_group_id,
    normalize_groups,
    normalize_prefix,
    subscription_topics,
)

GROUP = "0200000000F0"
PEER = "020000000002"
PREFIX = "homeassistant"
BASE = f"{PREFIX}/{GROUP}/light/{PEER}"


def energy(group=GROUP, peers=(PEER,)):
    return (
        bytes([len(peers), 5, 30])
        + bytes.fromhex(group)
        + b"".join(bytes.fromhex(peer) + bytes(3) for peer in peers)
    )


def native_config(**changes):
    return json.dumps({"device": {"manufacturer": "Lanbon"}, **changes}).encode()


@pytest.mark.parametrize("invalid", ["", "000000000000", "FFFFFFFFFFFF", "gg0000000000", None, 12])
def test_invalid_unspecified_and_broadcast_groups(invalid):
    with pytest.raises(ValueError, match="invalid_group_id"):
        normalize_group_id(invalid)


def test_normalization_is_literal_bounded_and_deduplicated():
    assert normalize_group_id(" 02:00:00:00:00:f0 ") == GROUP
    assert normalize_prefix(" /custom/native/ ") == "custom/native"
    assert normalize_groups(f"{GROUP.lower()}, {GROUP}\n02-00-00-00-00-02") == [GROUP, PEER]
    with pytest.raises(ValueError, match="too_many_groups"):
        normalize_groups([f"02000000{i:04X}" for i in range(17)])
    with pytest.raises(ValueError, match="no_groups"):
        normalize_groups("\n ,")


@pytest.mark.parametrize("prefix", ["", "#", "a/+", "a//b", "a\x00b", "a b", "é", "a" * 129, None])
def test_prefix_cannot_expand_topics_or_disagree_with_native_builder(prefix):
    with pytest.raises(ValueError, match="invalid_prefix"):
        normalize_prefix(prefix)


def test_energy_header_is_mesh_id_not_first_peer_or_physical_root():
    collector = DiscoveryCollector()
    assert collector.accept(ENERGY_TOPIC, energy())
    result = collector.result()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.group_id == GROUP
    assert candidate.peers == (PEER,)
    assert candidate.current_energy and candidate.current_seen
    assert candidate.sources == ("energy_header",)
    assert "current energy report" in candidate.label


def test_retained_energy_and_config_only_hint_without_current_evidence():
    collector = DiscoveryCollector()
    assert collector.accept(ENERGY_TOPIC, energy(), retained=True)
    assert collector.accept(BASE + "/config", native_config(), retained=True)
    before = collector.result().candidates[0]
    assert before.cached_seen and not before.current_seen and not before.current_energy
    assert "retained hint" in before.label
    assert collector.accept(ENERGY_TOPIC, energy())
    after = collector.result().candidates[0]
    assert after.current_energy and after.cached_seen
    assert not before.current_seen  # Results are snapshots, not mutable collector state.


def test_retained_flood_cannot_block_new_current_groups_or_replace_current():
    collector = DiscoveryCollector(max_groups=2)
    for i in range(20):
        collector.accept(ENERGY_TOPIC, energy(f"02000000{i:04X}"), retained=True)
    assert len(collector.result().candidates) == 2
    assert collector.accept(ENERGY_TOPIC, energy())
    assert collector.accept(ENERGY_TOPIC, energy("0200000000F2"))
    assert {item.group_id for item in collector.result().candidates} == {GROUP, "0200000000F2"}
    assert not collector.accept(ENERGY_TOPIC, energy("0200000000F4"), retained=True)


def test_cached_peer_hints_cannot_block_current_peer_at_capacity():
    collector = DiscoveryCollector()
    cached = tuple(f"02000000{i:04X}" for i in range(MAX_PEERS_PER_GROUP))
    collector.accept(ENERGY_TOPIC, energy(peers=cached), retained=True)
    collector.accept(ENERGY_TOPIC, energy(peers=("02000000FFFE",)))
    peers = collector.result().candidates[0].peers
    assert "02000000FFFE" in peers and len(peers) == MAX_PEERS_PER_GROUP


def test_stronger_energy_evicts_weaker_current_native_candidate():
    collector = DiscoveryCollector(max_groups=1)
    assert collector.accept(BASE + "/state", b"OFF")
    assert collector.accept(ENERGY_TOPIC, energy("0200000000F2"))
    assert collector.result().candidates[0].group_id == "0200000000F2"


@pytest.mark.parametrize(
    ("topic", "payload"),
    [
        (ENERGY_TOPIC, b"\x01\x05\x1e" + bytes(15)),
        (ENERGY_TOPIC, energy()[:-1]),
        (ENERGY_TOPIC, energy(peers=(PEER, PEER))),
        (ENERGY_TOPIC, energy(peers=("FFFFFFFFFFFF",))),
        (BASE + "/set", b"ON"),
        (BASE + "/brightnessSet", b"90"),
        (BASE + "/state", b"on"),
        (BASE + "/brightnessState", b"101"),
        (f"other/{GROUP}/light/{PEER}/state", b"ON"),
        ("lanbon/feedback/v1/custom/state", b"ON"),
        (f"lanbon/b/MAC:{GROUP}/s2m/cmdStatusSynchronous", bytes(11)),
        (ENERGY_TOPIC, b"x" * 8193),
    ],
)
def test_invalid_unscoped_or_command_packets_do_not_discover(topic, payload):
    collector = DiscoveryCollector()
    assert not collector.accept(topic, payload)
    assert not collector.result().candidates


def test_qualified_native_config_accepts_expanded_and_abbreviated_vendor_fields():
    assert native_config_hint(BASE + "/config", native_config()) == {
        "group_id": GROUP,
        "device_id": PEER,
        "discovery_prefix": PREFIX,
    }
    assert native_config_hint(BASE + "/config", b'{"dev":{"mf":"LANBON"}}')
    assert native_config_hint(BASE + "/config", b'{"uniq_id":"Lanbon_L8_synthetic"}')
    assert native_config_hint(BASE + "/config", native_config(state_topic=BASE + "/state"))


@pytest.mark.parametrize(
    "payload",
    [
        b"{}",
        b"[]",
        b'{"unique_id":"not_lanbon"}',
        b'{"device":{"manufacturer":"Other"}}',
        b'{"unique_id":"LanbonImpersonation"}',
        b'{"device":[]}',
        b"\xff",
        b"{",
        native_config(command_topic="elsewhere/set"),
        native_config(state_topic="elsewhere/state"),
    ],
)
def test_generic_or_redirected_discovery_configs_do_not_start_lanbon_flow(payload):
    assert native_config_hint(BASE + "/config", payload) is None


def test_config_namespace_must_match_and_all_scan_filters_are_narrow():
    assert native_config_hint("homeassistant/light/generic/config", native_config()) is None
    assert native_config_hint(BASE + "/config", native_config(), prefix="custom") is None
    filters = subscription_topics("custom/native")
    assert len(filters) == 8 and "#" not in "".join(filters)
    assert "custom/native/+/light/+/config" in filters
    assert ENERGY_TOPIC in filters
