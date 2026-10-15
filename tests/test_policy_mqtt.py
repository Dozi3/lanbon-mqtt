"""MQTT-only bootstrap, owner isolation and leased root-report reconciliation."""

import math
from dataclasses import replace

import pytest
from test_policy_adversarial import GROUP, PEER, P, configured, mqtt, record


def test_mqtt_only_startup_query_promotes_membership_after_reply():
    bindings = P.BindingRegistry(broker_scope="mqtt_only")
    with pytest.raises(P.PolicyError):
        bindings.qualify_group(GROUP, profile_confirmed=False)
    bindings.qualify_group(GROUP, profile_confirmed=True)
    assert not bindings.gateways and not bindings.members and not bindings.peer_owners
    policy = P.StatePolicy(bindings, clock=lambda: 2)
    token = policy.begin_provisional_query(GROUP, now=0, subscriptions_ready=True)
    with pytest.raises(P.PolicyError):
        policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=1)
    record(bindings, PEER, now=1)
    bindings.confirm_mqtt_group(
        GROUP,
        profile_confirmed=True,
        now=1,
        evidence_generation=bindings.evidence_generation,
    )
    bindings.associate_peer(GROUP, PEER, now=1, evidence_generation=bindings.evidence_generation)
    assert policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=2).applied
    current = policy.current(GROUP, PEER, "power")
    assert current.value == "OFF" and current.source is P.SourceKind.ROOT_CACHE
    assert not current.physical_state_verified and not current.peer_observation_age_known
    assert not bindings.gateways


def test_configured_group_registration_requires_explicit_trust_and_supported_profile():
    bindings = P.BindingRegistry()
    for kwargs in ({}, {"trusted": 1}, {"trusted": True, "route_profile": "unknown"}):
        with pytest.raises(P.PolicyError):
            bindings.register_configured_group(GROUP, **kwargs)
    assert not bindings.accepted_groups
    bindings.register_configured_group(GROUP, trusted=True)
    assert GROUP in bindings.accepted_groups and not bindings.members


def test_group_confirmation_requires_current_scope_and_profile():
    bindings = P.BindingRegistry()
    record(bindings, PEER, now=1)
    with pytest.raises(P.PolicyError):
        bindings.confirm_mqtt_group(
            GROUP,
            profile_confirmed=False,
            now=1,
            evidence_generation=bindings.evidence_generation,
        )
    bindings.reset_evidence()
    with pytest.raises(P.PolicyError):
        bindings.confirm_mqtt_group(
            GROUP,
            profile_confirmed=True,
            now=2,
            evidence_generation=bindings.evidence_generation,
        )


def test_matching_fresh_direct_keeps_its_source_then_falls_back_and_expires():
    bindings, _ = configured()
    policy = P.StatePolicy(bindings, max_report_age=10, root_cache_max_age=30)
    mqtt(policy, "power", "ON", now=0)
    token = policy.begin_query(GROUP, now=5, subscriptions_ready=True)
    result = policy.accept_root_cache(token, PEER, "power", "ON", retained=False, now=6)
    assert not result.applied and result.reason == "matching_fresh_direct_report_preferred"
    direct = policy.current(GROUP, PEER, "power", now=8)
    assert direct.source is P.SourceKind.DIRECT_MQTT and direct.received_monotonic == 0
    fallback = policy.current(GROUP, PEER, "power", now=11)
    assert fallback.source is P.SourceKind.ROOT_CACHE and fallback.received_monotonic == 6
    assert policy.current(GROUP, PEER, "power", now=37) is None


def test_snapshot_older_than_inflight_direct_cannot_become_later_fallback():
    bindings, _ = configured()
    policy = P.StatePolicy(bindings, max_report_age=10, root_cache_max_age=30)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    mqtt(policy, "power", "ON", now=2)
    result = policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=3)
    assert result.reason == "newer_direct_report_wins"
    assert policy.current(GROUP, PEER, "power", now=5).value == "ON"
    assert policy.current(GROUP, PEER, "power", now=13) is None
    assert policy.root_caches[(GROUP, PEER, "power")].value == "OFF"


def test_direct_after_snapshot_invalidates_that_snapshot_as_expiry_fallback():
    bindings, _ = configured()
    policy = P.StatePolicy(bindings, max_report_age=10, root_cache_max_age=30)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    policy.accept_root_cache(token, PEER, "brightness", 75, retained=False, now=1)
    mqtt(policy, "brightness", 25, now=2)
    assert policy.current(GROUP, PEER, "brightness", now=3).value == 25
    assert policy.current(GROUP, PEER, "brightness", now=13) is None


def test_copied_token_cannot_impersonate_the_original_local_window():
    _, policy = configured()
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    copied = replace(token)
    result = policy.accept_root_cache(copied, PEER, "power", "ON", retained=False, now=1)
    assert not result.applied and not policy.root_caches


def test_cached_only_capacity_never_blocks_unknown_nonallocating_reads():
    _, policy = configured(max_fields=1)
    mqtt(policy, "cached_only", 1, retained=True, now=1)
    assert policy.current(GROUP, PEER, "not_observed", now=2) is None
    assert not policy.fields


def test_root_cache_has_its_own_capacity_and_old_receipt_guard():
    _, policy = configured(max_fields=1)
    token = policy.begin_query(GROUP, now=0, subscriptions_ready=True)
    policy.accept_root_cache(token, PEER, "power", "ON", retained=False, now=3)
    assert not policy.accept_root_cache(token, PEER, "power", "OFF", retained=False, now=2).applied
    with pytest.raises(P.PolicyError, match="root_cache_limit"):
        policy.accept_root_cache(token, PEER, "other", 10, retained=False, now=4)
    assert policy.current(GROUP, PEER, "power", now=4).value == "ON"


def test_persistent_peer_cap_survives_membership_proof_resets():
    bindings = P.BindingRegistry(max_members=1)
    bindings.register_configured_group(GROUP, trusted=True)
    record(bindings, PEER, now=0)
    bindings.associate_peer(GROUP, PEER, now=0, evidence_generation=1)
    bindings.reset_evidence()
    other = "020000000004"
    record(bindings, other, now=1)
    with pytest.raises(P.PolicyError, match="accepted_peer_limit"):
        bindings.associate_peer(
            GROUP, other, now=1, evidence_generation=bindings.evidence_generation
        )
    assert len(bindings.peer_owners) == 1


def test_older_membership_evidence_cannot_erase_a_newer_valid_proof():
    bindings = P.BindingRegistry()
    record(bindings, PEER, now=200)
    assert not record(bindings, PEER, now=100)
    bindings.confirm_mqtt_group(
        GROUP,
        profile_confirmed=True,
        now=450,
        evidence_generation=bindings.evidence_generation,
    )


@pytest.mark.parametrize("age", [math.nan, math.inf, 0, -1, True, "1"])
def test_invalid_report_lease_is_rejected(age):
    bindings = P.BindingRegistry()
    with pytest.raises(P.PolicyError):
        P.StatePolicy(bindings, max_report_age=age)
