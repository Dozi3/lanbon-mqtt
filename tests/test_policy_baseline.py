"""Synthetic acceptance tests. No real addresses, HA, sockets or broker traffic."""

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "portable_policy",
    Path(__file__).resolve().parents[1] / "custom_components/lanbon_mqtt/policy.py",
)
P = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = P
SPEC.loader.exec_module(P)

GROUP = "102233445566"
ROOT = "020000000001"
PEER = "020000000002"
OTHER_PEER = "020000000003"
PARENT = "0200000000FE"
IP = "192.0.2.10"


def member(bindings, group, peer, *, retained, report_kind, now=0):
    return bindings.record_mqtt_member(
        group,
        peer,
        retained=retained,
        report_kind=report_kind,
        now=now,
        evidence_generation=bindings.evidence_generation,
        broker_scope=bindings.broker_scope,
    )


def confirm(bindings, candidate, *, source_approved, now=0):
    bindings.confirm_gateway(
        candidate,
        source_approved=source_approved,
        now=now,
        evidence_generation=bindings.evidence_generation,
    )
    bindings.confirm_mqtt_group(
        candidate.mesh_group,
        profile_confirmed=True,
        now=now,
        evidence_generation=bindings.evidence_generation,
    )


def associate(bindings, group, peer, *, now=0):
    return bindings.associate_peer(
        group, peer, now=now, evidence_generation=bindings.evidence_generation
    )


def metadata(root=ROOT, group=GROUP):
    return {
        "self_mac": root,
        "mesh_id": group,
        "parent_mac": PARENT,
        "version": "1.1.0",
    }


def notice(flag=1234, value=100, root=ROOT, peer=PEER):
    return P.parse_udp_notice(
        f"mac={root.lower()}\r\nflag={flag}\r\ntype=status[{peer}_{value}]\r\n".encode()
    )


@pytest.fixture
def registry():
    bindings = P.BindingRegistry()
    for peer in (ROOT, PEER, OTHER_PEER):
        member(bindings, GROUP, peer, retained=False, report_kind="status")
    confirm(bindings, P.metadata_candidate(metadata(), IP), source_approved=True)
    for peer in (PEER, OTHER_PEER):
        associate(bindings, GROUP, peer)
    return bindings


@pytest.mark.parametrize("flag", [-(2**31), -1, 0, 2**31 - 1, 2**32 - 1])
def test_signed_flag_preserved_and_uint32_normalization_is_arithmetic_only(flag):
    parsed = notice(flag)
    assert parsed.flag_raw == str(flag)
    assert parsed.flag_wire_value == flag
    assert parsed.flag_signed == (flag - 2**32 if flag >= 2**31 else flag)
    assert parsed.flag_u32 == flag & 0xFFFFFFFF
    assert parsed.root_mac == ROOT and parsed.device_mac == PEER
    assert parsed.output_raw == 100


@pytest.mark.parametrize(
    "payload",
    [
        b"x" * 129,
        b"mac=020000000001\nflag=1\ntype=status[020000000002_0]\n",
        b"mac=020000000001\r\nflag=4294967296\r\ntype=status[020000000002_0]\r\n",
        b"mac=020000000001\r\nflag=-2147483649\r\ntype=status[020000000002_0]\r\n",
        b"mac=020000000001\r\nflag=1\r\ntype=status[020000000002_256]\r\n",
        b"mac=020000000001\r\nflag=1\r\ntype=status[FFFFFFFFFFFF_0]\r\n",
        b"mac=020000000001\r\nflag=1\r\ntype=command[020000000002_0]\r\n",
    ],
)
def test_unqualified_or_malformed_udp_is_rejected(payload):
    with pytest.raises(P.PolicyError):
        P.parse_udp_notice(payload)


def test_metadata_namespaces_root_group_parent_and_version_are_independent():
    candidate = P.metadata_candidate(metadata(), IP)
    assert (candidate.root_mac, candidate.mesh_group, candidate.parent_mac) == (
        ROOT,
        GROUP,
        PARENT,
    )
    assert candidate.metadata_version == "1.1.0"
    assert "not Lanbon firmware" in candidate.namespace


def test_generic_discovery_or_metadata_alone_cannot_accept_a_gateway():
    bindings = P.BindingRegistry()
    candidate = P.metadata_candidate(metadata(), IP)
    with pytest.raises(P.PolicyError, match="source_confirmation"):
        confirm(bindings, candidate, source_approved=False)
    with pytest.raises(P.PolicyError, match="independently_observed"):
        confirm(bindings, candidate, source_approved=True)
    assert not bindings.gateways


def test_retained_unknown_retain_and_command_topics_do_not_establish_membership():
    bindings = P.BindingRegistry()
    for retained, report_kind in (
        (True, "status"),
        (None, "status"),
        (False, "command"),
        (False, "detail"),
    ):
        assert not member(bindings, GROUP, ROOT, retained=retained, report_kind=report_kind)
    assert not bindings.members


def test_two_meshes_work_but_duplicate_peer_or_root_claim_does_not(registry):
    group2, root2, ip2 = "202233445566", "020000000011", "192.0.2.11"
    for peer in (root2, PEER):
        member(registry, group2, peer, retained=False, report_kind="status_inventory")
    confirm(
        registry,
        P.metadata_candidate(metadata(root2, group2), ip2),
        source_approved=True,
    )
    with pytest.raises(P.PolicyError, match="multiple_groups"):
        associate(registry, group2, PEER)
    member(registry, group2, ROOT, retained=False, report_kind="status")
    with pytest.raises(P.PolicyError, match="gateway_change|conflicting"):
        confirm(
            registry,
            P.metadata_candidate(metadata(ROOT, group2), IP),
            source_approved=True,
        )
    assert registry.peer_owners[PEER] == GROUP


def test_udp_claim_needs_accepted_source_ip_root_and_peer(registry):
    for packet, source in (
        (notice(), "192.0.2.99"),
        (notice(root=PARENT), IP),
        (notice(peer="020000000099"), IP),
    ):
        with pytest.raises(P.PolicyError):
            registry.route_udp(packet, source)


def test_root_cache_is_labelled_as_cache_not_fresh_peer_output(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    result = policy.accept_root_cache(token, PEER, "output_raw", 100, retained=False, now=1)
    observation = policy.current(GROUP, PEER, "output_raw")
    assert result.applied and result.notify
    assert observation.source is P.SourceKind.ROOT_CACHE
    assert not observation.peer_observation_age_known
    assert not observation.physical_state_verified
    assert "no nonce" in token.correlation


def test_retained_and_unknown_retain_never_replace_current_off(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    policy.accept_mqtt(
        GROUP, PEER, "power", "OFF", retained=False, now=0, generation=policy.generation
    )
    for retained in (True, None):
        result = policy.accept_mqtt(
            GROUP,
            PEER,
            "power",
            "ON",
            retained=retained,
            now=1,
            generation=policy.generation,
        )
        assert result.reason == "cache_only" and not result.notify
        assert policy.current(GROUP, PEER, "power").value == "OFF"
    assert policy.notifications == 1
    assert policy.caches[(GROUP, PEER, "power")].source is P.SourceKind.UNKNOWN_RETAIN


def test_inflight_root_cache_cannot_overwrite_a_newer_unsolicited_report(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    policy.accept_mqtt(
        GROUP,
        PEER,
        "output_raw",
        0,
        retained=False,
        now=0.5,
        generation=policy.generation,
    )
    result = policy.accept_root_cache(token, PEER, "output_raw", 100, retained=False, now=1)
    assert result.reason == "newer_direct_report_wins" and not result.applied
    assert policy.current(GROUP, PEER, "output_raw").value == 0


def test_prequery_direct_can_be_reconciled_as_root_reported_cache(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    policy.accept_mqtt(
        GROUP,
        PEER,
        "output_raw",
        0,
        retained=False,
        now=0,
        generation=policy.generation,
    )
    token = policy.begin_query(GROUP, now=1, subscriptions_ready=True)
    result = policy.accept_root_cache(token, PEER, "output_raw", 100, retained=False, now=2)
    assert result.applied
    assert policy.current(GROUP, PEER, "output_raw").value == 100
    assert policy.current(GROUP, PEER, "output_raw").source is P.SourceKind.ROOT_CACHE
    assert policy.root_caches[(GROUP, PEER, "output_raw")].value == 100
    assert policy.root_caches[(GROUP, PEER, "output_raw")].source is P.SourceKind.ROOT_CACHE


def test_capture_gap_invalidates_state_old_messages_and_query(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    old_generation = policy.generation
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    policy.accept_root_cache(token, PEER, "output_raw", 100, retained=False, now=1)
    policy.reset_capture(active=True)
    assert policy.current(GROUP, PEER, "output_raw") is None
    assert not policy.accept_mqtt(
        GROUP, PEER, "output_raw", 100, retained=False, now=2, generation=old_generation
    ).applied
    assert not policy.accept_root_cache(
        token, PEER, "output_raw", 100, retained=False, now=2
    ).applied
    assert not policy.accept_udp(notice(), IP, now=2, generation=old_generation).applied


def test_five_udp_copies_produce_one_change_and_do_not_refresh_report_time(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    first = policy.accept_udp(notice(), IP, now=0, generation=policy.generation)
    assert first.applied and first.notify
    for now in (0.05, 0.15, 0.3, 0.6):
        result = policy.accept_udp(notice(), IP, now=now, generation=policy.generation)
        assert result.reason == "duplicate_udp_notice" and not result.notify
    assert policy.notifications == 1
    assert policy.current(GROUP, PEER, "output_raw").received_monotonic == 0


def test_opaque_tick_does_not_block_reboot_or_same_tick_changed_value(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    policy.accept_udp(notice(100, 100), IP, now=0, generation=policy.generation)
    policy.accept_udp(notice(101, 0), IP, now=1, generation=policy.generation)
    old = policy.accept_udp(notice(99, 100), IP, now=2, generation=policy.generation)
    conflict = policy.accept_udp(notice(101, 100), IP, now=3, generation=policy.generation)
    assert old.applied and conflict.applied
    assert policy.current(GROUP, PEER, "output_raw").value == 100


def test_signed_boundary_and_uint32_wrap_are_handled_without_timestamp_claim(registry):
    for first, second in ((2**31 - 1, -(2**31)), (-1, 0)):
        policy = P.StatePolicy(registry, clock=lambda: 100)
        assert policy.accept_udp(
            notice(first, 100), IP, now=0, generation=policy.generation
        ).applied
        assert policy.accept_udp(notice(second, 0), IP, now=1, generation=policy.generation).applied
        assert policy.current(GROUP, PEER, "output_raw").value == 0


def test_same_tick_can_report_two_different_peers(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    assert policy.accept_udp(notice(1, 0), IP, now=0, generation=policy.generation).applied
    assert policy.accept_udp(
        notice(1, 100, peer=OTHER_PEER), IP, now=0.1, generation=policy.generation
    ).applied


def test_query_requires_real_readiness_has_bounds_and_cannot_be_flooded(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    with pytest.raises(P.PolicyError, match="readiness"):
        policy.begin_query(GROUP, now=0, subscriptions_ready=False)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    with pytest.raises(P.PolicyError, match="already_outstanding"):
        policy.begin_query(GROUP, now=1, subscriptions_ready=True)
    with pytest.raises(P.PolicyError, match="rate_limited"):
        policy.begin_query(GROUP, now=11, subscriptions_ready=True)
    assert not policy.accept_root_cache(token, PEER, "output_raw", 0, retained=True, now=1).applied
    assert not policy.accept_root_cache(
        token, PEER, "output_raw", 0, retained=False, now=11
    ).applied
    assert (
        policy.begin_query(GROUP, now=61, subscriptions_ready=True).local_serial
        != token.local_serial
    )


def test_query_builder_is_fixed_readonly_intent_and_never_arbitrary_opcode():
    intent = P.build_all_status_query(GROUP, route_profile="observed_legacy_lanbon_a_m2s_v1")
    assert intent.payload == b"\x11" + b"\0" * 7
    assert intent.topic == f"lanbon/a/MAC:{GROUP}/m2s/cmdQuery"
    assert intent.retain is False and intent.qos == 1
    with pytest.raises(P.PolicyError, match="unqualified"):
        P.build_all_status_query(GROUP, route_profile="specified_device_0x12")


def test_group_and_field_resource_limits_are_bounded(registry):
    limited = P.BindingRegistry(max_groups=1, max_members=1)
    member(limited, GROUP, ROOT, retained=False, report_kind="status")
    with pytest.raises(P.PolicyError, match="member_limit"):
        member(limited, GROUP, PEER, retained=False, report_kind="status")
    with pytest.raises(P.PolicyError, match="group_limit"):
        member(limited, "202233445566", ROOT, retained=False, report_kind="status")
    policy = P.StatePolicy(registry, max_fields=1)
    policy.accept_mqtt(
        GROUP, PEER, "power", "OFF", retained=False, now=0, generation=policy.generation
    )
    with pytest.raises(P.PolicyError, match="field_limit"):
        policy.accept_mqtt(
            GROUP,
            OTHER_PEER,
            "power",
            "OFF",
            retained=False,
            now=0,
            generation=policy.generation,
        )


def test_old_membership_cannot_qualify_new_binding_after_capture_reset(registry):
    policy = P.StatePolicy(registry, clock=lambda: 100)
    policy.reset_capture(active=True)
    with pytest.raises(P.PolicyError, match="independently_observed"):
        confirm(registry, P.metadata_candidate(metadata(), IP), source_approved=True, now=10)
    member(registry, GROUP, ROOT, retained=False, report_kind="status", now=10)
    confirm(registry, P.metadata_candidate(metadata(), IP), source_approved=True, now=11)


def test_membership_evidence_has_age_generation_and_broker_scope(registry):
    with pytest.raises(P.PolicyError, match="fresh_scoped"):
        confirm(
            registry,
            P.metadata_candidate(metadata(), IP),
            source_approved=True,
            now=301,
        )
    assert not registry.record_mqtt_member(
        GROUP,
        ROOT,
        retained=False,
        report_kind="status",
        now=10,
        evidence_generation=registry.evidence_generation - 1,
        broker_scope=registry.broker_scope,
    )
    assert not registry.record_mqtt_member(
        GROUP,
        ROOT,
        retained=False,
        report_kind="status",
        now=10,
        evidence_generation=registry.evidence_generation,
        broker_scope="other_broker",
    )


def test_duplicate_window_is_time_bounded_and_root_reboot_needs_no_receiver_gap(
    registry,
):
    policy = P.StatePolicy(registry, udp_duplicate_window=2, clock=lambda: 100)
    policy.accept_udp(notice(1000, 100), IP, now=0, generation=policy.generation)
    assert not policy.accept_udp(notice(1000, 100), IP, now=1, generation=policy.generation).applied
    # Flag is opaque by default: a root reboot with a smaller tick is accepted.
    assert policy.accept_udp(notice(1, 0), IP, now=1.5, generation=policy.generation).applied
    assert policy.current(GROUP, PEER, "output_raw").value == 0
    # Identical payload outside the receipt window is not globally suppressed.
    assert policy.accept_udp(notice(1000, 100), IP, now=3, generation=policy.generation).applied
