"""Independent offline invariants; never imports HA or starts network clients."""

from __future__ import annotations

import ast
import importlib.util
import math
import sys
from pathlib import Path

import pytest

TARGET = Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/policy.py"
SPEC = importlib.util.spec_from_file_location("lanbon_policy_adversarial_target", TARGET)
P = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = P
SPEC.loader.exec_module(P)

GROUP = "102233445566"
ROOT = "020000000001"
PEER = "020000000002"
IP = "192.0.2.10"


def candidate(root=ROOT, group=GROUP, ip=IP):
    return P.metadata_candidate({"self_mac": root, "mesh_id": group, "version": "descriptor"}, ip)


def record(bindings, peer, *, group=GROUP, now=0, generation=None, scope=None):
    return bindings.record_mqtt_member(
        group,
        peer,
        retained=False,
        report_kind="status_inventory",
        now=now,
        evidence_generation=bindings.evidence_generation if generation is None else generation,
        broker_scope=bindings.broker_scope if scope is None else scope,
    )


def configured(*, scope="broker_a", max_groups=16, max_members=256, max_fields=4096):
    bindings = P.BindingRegistry(broker_scope=scope, max_groups=max_groups, max_members=max_members)
    for peer in (ROOT, PEER):
        record(bindings, peer)
    bindings.confirm_gateway(
        candidate(), source_approved=True, now=0, evidence_generation=bindings.evidence_generation
    )
    bindings.confirm_mqtt_group(
        GROUP, profile_confirmed=True, now=0, evidence_generation=bindings.evidence_generation
    )
    bindings.associate_peer(GROUP, PEER, now=0, evidence_generation=bindings.evidence_generation)
    return bindings, P.StatePolicy(bindings, max_fields=max_fields, clock=lambda: 100)


def mqtt(policy, field, value, *, now=0, retained=False, generation=None):
    return policy.accept_mqtt(
        GROUP,
        PEER,
        field,
        value,
        retained=retained,
        now=now,
        generation=policy.generation if generation is None else generation,
    )


def notice(flag=1, value=100, *, root=ROOT, peer=PEER):
    return P.parse_udp_notice(
        f"mac={root}\r\nflag={flag}\r\ntype=status[{peer}_{value}]\r\n".encode()
    )


def test_cache_pressure_must_not_block_existing_live_field():
    """Cache-only input cannot deny a newer report to an already admitted live field."""
    _, policy = configured(max_fields=2)
    mqtt(policy, "power", "OFF", now=1)
    mqtt(policy, "cached_a", 1, now=2, retained=True)
    mqtt(policy, "cached_b", 1, now=3, retained=None)
    update = mqtt(policy, "power", "ON", now=4)
    assert update.applied and policy.current(GROUP, PEER, "power").value == "ON"


def test_cache_pressure_must_not_block_reading_existing_live_field():
    _, policy = configured(max_fields=2)
    mqtt(policy, "power", "OFF", now=1)
    mqtt(policy, "cached_a", 1, now=2, retained=True)
    mqtt(policy, "cached_b", 1, now=3, retained=None)
    assert policy.current(GROUP, PEER, "power").value == "OFF"


def test_older_query_receipt_cannot_roll_back_latest_root_cache():
    """A delayed decoder completion must not roll receipt/provenance backwards."""
    _, policy = configured()
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    policy.accept_root_cache(token, PEER, "output_raw", 75, retained=False, now=3)
    older = policy.accept_root_cache(token, PEER, "output_raw", 25, retained=False, now=2)
    assert not older.applied
    latest = policy.root_caches[(GROUP, PEER, "output_raw")]
    assert (latest.value, latest.received_monotonic) == (75, 3)


def test_accepted_gateway_capacity_survives_evidence_reset():
    """A capture reset does not make persistent accepted configuration unbounded."""
    bindings = P.BindingRegistry(max_groups=1, max_members=2)
    for index in range(3):
        group = f"{0x102233445560 + index:012X}"
        root = f"{0x020000000010 + index * 2:012X}"
        record(bindings, root, group=group, now=index)
        try:
            bindings.confirm_gateway(
                candidate(root, group, f"192.0.2.{10 + index}"),
                source_approved=True,
                now=index,
                evidence_generation=bindings.evidence_generation,
            )
        except P.PolicyError:
            pass
        assert len(bindings.gateways) <= 1, "max_groups must also cap accepted configuration"
        bindings.reset_evidence()


def test_same_shaped_window_from_another_broker_scope_is_not_this_query():
    """Local query-window ownership must not depend on coincidentally equal fields."""
    _, a = configured(scope="broker_a")
    _, b = configured(scope="broker_b")
    token_a = a.begin_query(GROUP, now=10, subscriptions_ready=True)
    b.begin_query(GROUP, now=10, subscriptions_ready=True)
    result = b.accept_root_cache(token_a, PEER, "output_raw", 99, retained=False, now=11)
    assert not result.applied and not b.root_caches, (
        "foreign local-window token crossed broker scope"
    )


def test_later_field_does_not_block_other_field_snapshot():
    _, policy = configured()
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    mqtt(policy, "brightness", 75, now=1)
    snapshot = policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=2)
    assert snapshot.applied and policy.current(GROUP, PEER, "power").value == "OFF"


def test_generation_gap_recovery_is_labelled_cache_not_peer_verification():
    _, policy = configured()
    mqtt(policy, "power", "ON", now=1)
    old_generation = policy.generation
    policy.reset_capture(active=True)
    assert policy.current(GROUP, PEER, "power") is None
    assert not mqtt(policy, "power", "ON", now=2, generation=old_generation).applied
    token = policy.begin_query(GROUP, now=3, subscriptions_ready=True)
    recovered = policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=4)
    current = policy.current(GROUP, PEER, "power")
    assert recovered.applied and current.value == "OFF"
    assert current.source is P.SourceKind.ROOT_CACHE
    assert not current.physical_state_verified and not current.peer_observation_age_known


def test_query_reconciles_a_missed_event_with_root_cache_provenance():
    """Root-approved product policy allows a later root report to reconcile old direct data."""
    _, policy = configured()
    mqtt(policy, "power", "ON", now=1)
    token = policy.begin_query(GROUP, now=2, subscriptions_ready=True)
    result = policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=3)
    assert result.applied
    current = policy.current(GROUP, PEER, "power")
    assert current.value == "OFF" and current.source is P.SourceKind.ROOT_CACHE
    assert not current.physical_state_verified and not current.peer_observation_age_known
    assert policy.root_caches[(GROUP, PEER, "power")].value == "OFF"


@pytest.mark.parametrize("retained", [True, None, 0, 1, "false"])
def test_only_literal_false_can_promote_mqtt_state(retained):
    _, policy = configured()
    mqtt(policy, "power", "OFF", now=0)
    result = mqtt(policy, "power", "ON", now=1, retained=retained)
    assert result.reason == "cache_only"
    assert policy.current(GROUP, PEER, "power").value == "OFF"


@pytest.mark.parametrize("now", [math.nan, math.inf, -math.inf, True, None, "1"])
def test_nonfinite_or_non_numeric_receipt_time_cannot_create_live_state(now):
    _, policy = configured()
    with pytest.raises(P.PolicyError):
        mqtt(policy, "power", "ON", now=now)
    assert not policy.fields


@pytest.mark.parametrize("timeout", [math.nan, math.inf, 0, -1, 61])
def test_query_duration_is_finite_positive_and_bounded(timeout):
    _, policy = configured()
    with pytest.raises(P.PolicyError):
        policy.begin_query(GROUP, now=0, subscriptions_ready=True, timeout=timeout)
    assert not policy.queries


@pytest.mark.parametrize("readiness", [False, None, 0, 1, "true"])
def test_truthy_values_are_not_subscription_readiness(readiness):
    _, policy = configured()
    with pytest.raises(P.PolicyError):
        policy.begin_query(GROUP, now=0, subscriptions_ready=readiness)
    assert not policy.queries


def test_expired_and_superseded_query_cannot_create_cache():
    _, policy = configured()
    old = policy.begin_query(GROUP, now=0, subscriptions_ready=True, timeout=5)
    assert not policy.accept_root_cache(old, PEER, "power", "ON", retained=False, now=5.01).applied
    assert not policy.root_caches
    new = policy.begin_query(GROUP, now=60, subscriptions_ready=True)
    assert not policy.accept_root_cache(old, PEER, "power", "ON", retained=False, now=61).applied
    assert policy.accept_root_cache(new, PEER, "power", "OFF", retained=False, now=61).applied


def test_broker_scope_and_membership_clock_cannot_be_invented():
    bindings = P.BindingRegistry(broker_scope="broker_a")
    assert not record(bindings, ROOT, scope="broker_b")
    record(bindings, ROOT, now=100)
    for now in (99, 401):
        with pytest.raises(P.PolicyError):
            bindings.confirm_gateway(
                candidate(),
                source_approved=True,
                now=now,
                evidence_generation=bindings.evidence_generation,
            )
    assert not bindings.gateways


def test_capture_gap_prevents_old_membership_from_new_binding():
    bindings = P.BindingRegistry()
    record(bindings, ROOT)
    generation = bindings.evidence_generation
    policy = P.StatePolicy(bindings)
    policy.reset_capture(active=True)
    assert not record(bindings, ROOT, now=1, generation=generation)
    with pytest.raises(P.PolicyError):
        bindings.confirm_gateway(
            candidate(),
            source_approved=True,
            now=1,
            evidence_generation=bindings.evidence_generation,
        )


def test_udp_copy_window_survives_same_tick_and_root_reboot():
    _, policy = configured()
    assert policy.accept_udp(notice(2**32 - 1, 100), IP, now=0, generation=1).applied
    assert policy.accept_udp(notice(0, 0), IP, now=1, generation=1).applied
    assert policy.accept_udp(notice(0, 50), IP, now=1.1, generation=1).applied
    same = notice(0, 50)
    assert not policy.accept_udp(same, IP, now=1.2, generation=1).applied
    assert policy.current(GROUP, PEER, "output_raw").received_monotonic == 1.1
    assert policy.accept_udp(same, IP, now=3.2, generation=1).applied


def test_udp_exact_raw_flag_is_preserved_instead_of_assumed_epoch():
    _, policy = configured()
    assert policy.accept_udp(notice("0001", 50), IP, now=0, generation=1).applied
    assert policy.accept_udp(notice("1", 50), IP, now=0.1, generation=1).applied
    assert policy.current(GROUP, PEER, "output_raw").physical_state_verified is False


def test_udp_duplicate_memory_stays_bounded_under_burst():
    _, policy = configured()
    for index in range(2048):
        policy.accept_udp(notice(index, index % 101), IP, now=index / 10000, generation=1)
    assert len(policy._seen_udp) <= 256
    assert policy.current(GROUP, PEER, "output_raw").value == 2047 % 101


def test_two_mesh_peers_and_udp_sources_do_not_cross():
    bindings, policy = configured()
    group2, root2, peer2, ip2 = "202233445566", "020000000011", "020000000012", "192.0.2.11"
    for peer in (root2, peer2):
        record(bindings, peer, group=group2)
    bindings.confirm_gateway(
        candidate(root2, group2, ip2),
        source_approved=True,
        now=0,
        evidence_generation=bindings.evidence_generation,
    )
    bindings.confirm_mqtt_group(
        group2, profile_confirmed=True, now=0, evidence_generation=bindings.evidence_generation
    )
    bindings.associate_peer(group2, peer2, now=0, evidence_generation=bindings.evidence_generation)
    for packet, source in [(notice(peer=peer2), IP), (notice(root=root2), ip2), (notice(), ip2)]:
        with pytest.raises(P.PolicyError):
            policy.accept_udp(packet, source, now=1, generation=policy.generation)
    assert not policy.fields


def test_same_value_refresh_is_not_a_material_notification():
    _, policy = configured()
    assert mqtt(policy, "power", "OFF", now=0).notify
    assert not mqtt(policy, "power", "OFF", now=1).notify
    assert policy.current(GROUP, PEER, "power").received_monotonic == 1
    assert policy.notifications == 1


def test_module_has_no_network_or_home_assistant_runtime_imports():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(item.name.split(".")[0] for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
    assert not imports & {
        "socket",
        "ssl",
        "requests",
        "httpx",
        "aiohttp",
        "homeassistant",
        "paho",
        "subprocess",
        "urllib",
    }
