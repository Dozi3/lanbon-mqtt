"""Offline Lanbon discovery/state-policy prototype; no HA or network operations.

The caller supplies captured records and explicit accepted-source decisions.
Results are observations and query *intent*, never commands or physical proof.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Any

SCHEMA_VERSION = 1
QUERY_PROFILE = "observed_legacy_lanbon_a_m2s_v1"
UDP_PATTERN = re.compile(
    rb"mac=([0-9A-Fa-f]{12})\r\nflag=(-?[0-9]{1,10})\r\n"
    rb"type=status\[([0-9A-Fa-f]{12})_([0-9]{1,3})\]\r\n"
)
SUPPORTED_REPORTS = frozenset({"power", "brightness", "status", "status_inventory"})


class PolicyError(ValueError):
    """A bounded validation or policy decision rejected this proposal."""


class SourceKind(StrEnum):
    DIRECT_MQTT = "unsolicited_mqtt_report"
    DIRECT_UDP = "unsolicited_udp_report"
    ROOT_CACHE = "root_cache_query_response"
    RETAINED_CACHE = "retained_mqtt_cache"
    UNKNOWN_RETAIN = "unknown_retain_cache"


def identifier(value: str, *, physical: bool = True) -> str:
    """Normalize accepted UI separators, never arbitrary whitespace or padding."""
    if not isinstance(value, str):
        raise PolicyError("identifier_not_text")
    normalized = value.upper().replace(":", "").replace("-", "")
    if not re.fullmatch(r"[0-9A-F]{12}", normalized):
        raise PolicyError("invalid_identifier")
    if normalized in {"000000000000", "FFFFFFFFFFFF"}:
        raise PolicyError("unspecified_or_broadcast_identifier")
    if physical and int(normalized[:2], 16) & 1:
        raise PolicyError("multicast_physical_identifier")
    return normalized


@dataclass(frozen=True, slots=True)
class UdpNotice:
    root_mac: str
    device_mac: str
    flag_raw: str
    flag_wire_value: int
    flag_signed: int
    flag_u32: int
    output_raw: int
    payload_sha256: str


def parse_udp_notice(payload: bytes) -> UdpNotice:
    """Decode only the observed bounded plaintext notice shape and signed flag."""
    if not isinstance(payload, bytes) or len(payload) > 128:
        raise PolicyError("invalid_udp_payload_type_or_size")
    match = UDP_PATTERN.fullmatch(payload)
    if match is None:
        raise PolicyError("unsupported_udp_grammar")
    root, flag_raw, peer, output_raw = (field.decode("ascii") for field in match.groups())
    flag, output = int(flag_raw), int(output_raw)
    if not -(2**31) <= flag <= 2**32 - 1:
        raise PolicyError("flag_outside_signed_or_unsigned32_profile")
    if not 0 <= output <= 255:
        raise PolicyError("output_outside_byte_profile")
    return UdpNotice(
        identifier(root),
        identifier(peer),
        flag_raw,
        flag,
        (flag & 0xFFFFFFFF) - 2**32 if flag & 0x80000000 else flag,
        flag & 0xFFFFFFFF,
        output,
        hashlib.sha256(payload).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class GatewayCandidate:
    root_mac: str
    mesh_group: str
    parent_mac: str | None
    source_ip: str
    metadata_version: str | None
    namespace: str = "ESP-MDF/Mlink metadata; not Lanbon firmware"


def metadata_candidate(metadata: dict[str, Any], source_ip: str) -> GatewayCandidate:
    """Correlate actual fields, without treating generic mDNS as vendor proof."""
    if not isinstance(metadata, dict):
        raise PolicyError("metadata_not_object")
    try:
        address = ipaddress.ip_address(source_ip)
        if address.is_multicast or address.is_unspecified:
            raise PolicyError("invalid_gateway_source_ip")
        parent = metadata.get("parent_mac")
        parent = None if parent in (None, "", "000000000000") else identifier(parent)
        version = metadata.get("version")
        if version is not None and (not isinstance(version, str) or len(version) > 64):
            raise PolicyError("invalid_metadata_version")
        return GatewayCandidate(
            identifier(metadata["self_mac"]),
            identifier(metadata["mesh_id"], physical=False),
            parent,
            str(address),
            version,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, PolicyError):
            raise
        raise PolicyError("invalid_gateway_metadata") from error


class BindingRegistry:
    """Explicitly accepted associations; untrusted discovery cannot replace them."""

    def __init__(
        self,
        *,
        max_groups: int = 16,
        max_members: int = 256,
        broker_scope: str = "configured_ha_mqtt",
    ):
        for limit in (max_groups, max_members):
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise PolicyError("invalid_registry_limit")
        self.max_groups, self.max_members = max_groups, max_members
        self.members: dict[str, set[str]] = {}
        self.accepted_groups: dict[str, str] = {}
        self.gateways: dict[str, GatewayCandidate] = {}
        self.peer_owners: dict[str, str] = {}
        self.broker_scope = broker_scope
        self.evidence_generation = 1
        self.membership_evidence: dict[tuple[str, str], tuple[float, int, str]] = {}

    def _group_capacity(self, group: str) -> None:
        configured = self.accepted_groups.keys() | self.gateways.keys()
        if group not in configured and len(configured) >= self.max_groups:
            raise PolicyError("accepted_group_limit")

    def register_configured_group(
        self, group: str, *, route_profile: str = QUERY_PROFILE, trusted: bool = False
    ) -> None:
        """Register user-confirmed entry configuration, never membership or live state."""
        group = identifier(group, physical=False)
        if trusted is not True or route_profile != QUERY_PROFILE:
            raise PolicyError("confirmed_supported_group_profile_required")
        self._group_capacity(group)
        previous = self.accepted_groups.get(group)
        if previous is not None and previous != route_profile:
            raise PolicyError("group_profile_change_requires_reconfirmation")
        self.accepted_groups[group] = route_profile

    def qualify_group(
        self, group: str, *, profile_confirmed: bool, route_profile: str = QUERY_PROFILE
    ) -> None:
        """Permit bounded startup discovery of an explicitly confirmed legacy group."""
        self.register_configured_group(
            group, route_profile=route_profile, trusted=profile_confirmed
        )

    def confirm_mqtt_group(
        self,
        group: str,
        *,
        profile_confirmed: bool,
        now: float,
        evidence_generation: int,
        route_profile: str = QUERY_PROFILE,
    ) -> None:
        """Confirm current scoped reports independently of an HTTP gateway."""
        group = identifier(group, physical=False)
        if profile_confirmed is not True or route_profile != QUERY_PROFILE:
            raise PolicyError("confirmed_supported_group_profile_required")
        members = self.members.get(group, set())
        if not members:
            raise PolicyError("group_not_independently_observed_in_mqtt")
        for device in members:
            try:
                self._fresh_member(group, device, now=now, evidence_generation=evidence_generation)
            except PolicyError:
                continue
            self.register_configured_group(group, route_profile=route_profile, trusted=True)
            return
        raise PolicyError("fresh_scoped_mqtt_membership_required")

    def reset_evidence(self) -> None:
        """Accepted configuration persists; old observations cannot qualify new bindings."""
        self.evidence_generation += 1
        self.members.clear()
        self.membership_evidence.clear()

    def _fresh_member(self, group, device, *, now, evidence_generation, max_age=300):
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise PolicyError("invalid_membership_receipt_clock")
        evidence = self.membership_evidence.get((group, device))
        if (
            evidence is None
            or evidence_generation != self.evidence_generation
            or evidence[1] != evidence_generation
            or evidence[2] != self.broker_scope
            or not 0 <= now - evidence[0] <= max_age
        ):
            raise PolicyError("fresh_scoped_mqtt_membership_required")

    def record_mqtt_member(
        self,
        group: str,
        device: str,
        *,
        retained: bool | None,
        report_kind: str,
        now: float,
        evidence_generation: int,
        broker_scope: str,
    ) -> bool:
        group, device = identifier(group, physical=False), identifier(device)
        if retained is not False or report_kind not in SUPPORTED_REPORTS:
            return False
        if evidence_generation != self.evidence_generation or broker_scope != self.broker_scope:
            return False
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise PolicyError("invalid_membership_receipt_clock")
        if group not in self.members:
            if len(self.members) >= self.max_groups:
                raise PolicyError("group_limit")
            self.members[group] = set()
        if device not in self.members[group] and len(self.members[group]) >= self.max_members:
            raise PolicyError("member_limit")
        previous_evidence = self.membership_evidence.get((group, device))
        if previous_evidence and now < previous_evidence[0]:
            return False
        self.members[group].add(device)
        self.membership_evidence[(group, device)] = (
            now,
            evidence_generation,
            broker_scope,
        )
        return True

    def confirm_gateway(
        self,
        candidate: GatewayCandidate,
        *,
        source_approved: bool,
        now: float,
        evidence_generation: int,
    ) -> None:
        if source_approved is not True:
            raise PolicyError("source_confirmation_required")
        if candidate.root_mac not in self.members.get(candidate.mesh_group, set()):
            raise PolicyError("root_not_independently_observed_in_mqtt_group")
        self._fresh_member(
            candidate.mesh_group,
            candidate.root_mac,
            now=now,
            evidence_generation=evidence_generation,
        )
        previous = self.gateways.get(candidate.mesh_group)
        if previous is not None and previous != candidate:
            raise PolicyError("gateway_change_requires_reconfirmation")
        for group, existing in self.gateways.items():
            if group != candidate.mesh_group and (
                existing.root_mac == candidate.root_mac or existing.source_ip == candidate.source_ip
            ):
                raise PolicyError("conflicting_root_or_source_association")
        self._group_capacity(candidate.mesh_group)
        self.gateways[candidate.mesh_group] = candidate

    def associate_peer(
        self, group: str, device: str, *, now: float, evidence_generation: int
    ) -> None:
        group, device = identifier(group, physical=False), identifier(device)
        if group not in self.accepted_groups or device not in self.members.get(group, set()):
            raise PolicyError("peer_membership_unconfirmed")
        self._fresh_member(group, device, now=now, evidence_generation=evidence_generation)
        previous = self.peer_owners.get(device)
        if previous is not None and previous != group:
            raise PolicyError("peer_claimed_by_multiple_groups")
        if previous is None:
            if sum(owner == group for owner in self.peer_owners.values()) >= self.max_members:
                raise PolicyError("accepted_peer_limit")
        self.peer_owners[device] = group

    def check_peer(self, group: str, device: str) -> tuple[str, str]:
        group, device = identifier(group, physical=False), identifier(device)
        if self.peer_owners.get(device) != group or group not in self.accepted_groups:
            raise PolicyError("unassociated_peer")
        return group, device

    def route_udp(self, notice: UdpNotice, source_ip: str) -> tuple[str, str]:
        address = str(ipaddress.ip_address(source_ip))
        matches = [
            candidate
            for candidate in self.gateways.values()
            if candidate.root_mac == notice.root_mac and candidate.source_ip == address
        ]
        if len(matches) != 1:
            raise PolicyError("udp_source_not_accepted_gateway")
        return self.check_peer(matches[0].mesh_group, notice.device_mac)


@dataclass(frozen=True, slots=True)
class Observation:
    value: int | float | str
    source: SourceKind
    received_monotonic: float
    generation: int
    peer_observation_age_known: bool = False
    physical_state_verified: bool = False


@dataclass(frozen=True, slots=True)
class Change:
    applied: bool
    notify: bool
    reason: str


@dataclass(frozen=True, slots=True)
class QueryToken:
    group: str
    generation: int
    local_serial: int
    start_revision: int
    started: float
    expires: float
    correlation: str = "local_window_only; wire has no nonce"
    owner: object | None = None
    broker_scope: str = ""


@dataclass(frozen=True, slots=True)
class QueryIntent:
    topic: str
    payload: bytes
    qos: int = 1
    retain: bool = False
    operation: str = "read_root_cached_inventory"


def build_all_status_query(group: str, *, route_profile: str) -> QueryIntent:
    if route_profile != QUERY_PROFILE:
        raise PolicyError("unqualified_query_route_profile")
    group = identifier(group, physical=False)
    return QueryIntent(f"lanbon/a/MAC:{group}/m2s/cmdQuery", bytes.fromhex("1100000000000000"))


class StatePolicy:
    """Leased reported state with explicit query provenance and no publication.

    Transport adapters own actual subscription readiness and capture generations.
    Root cache is a root report, never a fresh child RF or physical confirmation.
    """

    def __init__(
        self,
        bindings: BindingRegistry,
        *,
        max_fields: int = 4096,
        udp_duplicate_window: float = 2,
        max_report_age: float = 300,
        root_cache_max_age: float | None = None,
        clock: Callable[[], float] = monotonic,
    ):
        if isinstance(max_fields, bool) or not isinstance(max_fields, int) or max_fields <= 0:
            raise PolicyError("invalid_field_limit")
        self.bindings, self.max_fields = bindings, max_fields
        self.generation = 1
        self.active = True
        self.clock = clock
        self.max_report_age = self._duration(max_report_age)
        self.root_cache_max_age = self._duration(
            max_report_age if root_cache_max_age is None else root_cache_max_age
        )
        self._query_owner = object()
        self.revision = 0
        self.fields: dict[tuple[str, str, str], Observation] = {}
        self.caches: dict[tuple[str, str, str], Observation] = {}
        self.root_caches: dict[tuple[str, str, str], Observation] = {}
        self._root_cache_start_revisions: dict[tuple[str, str, str], int] = {}
        self.direct_revisions: dict[tuple[str, str, str], int] = {}
        self.queries: dict[str, QueryToken] = {}
        self.last_query: dict[str, float] = {}
        self.notifications = 0
        self._serial = 0
        self.udp_duplicate_window = self._duration(udp_duplicate_window)
        if self.udp_duplicate_window > 60:
            raise PolicyError("invalid_udp_duplicate_window")
        self._seen_udp: OrderedDict[tuple[Any, ...], float] = OrderedDict()

    def reset_capture(self, *, active: bool) -> None:
        self.generation += 1
        self.active = active
        self.queries.clear()
        self._seen_udp.clear()
        self.bindings.reset_evidence()

    def _key(self, group: str, peer: str, field: str) -> tuple[str, str, str]:
        group, peer = self.bindings.check_peer(group, peer)
        if not isinstance(field, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field):
            raise PolicyError("invalid_field_name")
        key = (group, peer, field)
        return key

    @staticmethod
    def _duration(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PolicyError("invalid_lease_duration")
        if not math.isfinite(value) or value <= 0:
            raise PolicyError("invalid_lease_duration")
        return float(value)

    def _capacity(self, target: dict, key: tuple, reason: str) -> None:
        if key not in target and len(target) >= self.max_fields:
            raise PolicyError(reason)

    def _fresh(self, observation: Observation, now: float) -> bool:
        age_limit = (
            self.root_cache_max_age
            if observation.source is SourceKind.ROOT_CACHE
            else self.max_report_age
        )
        return (
            self.active
            and observation.generation == self.generation
            and 0 <= now - observation.received_monotonic <= age_limit
        )

    @staticmethod
    def _clock(now: float) -> float:
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise PolicyError("invalid_monotonic_time")
        return float(now)

    @staticmethod
    def _value(value: float | str) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise PolicyError("unsupported_observation_value")
        if isinstance(value, float) and not math.isfinite(value):
            raise PolicyError("nonfinite_observation")
        if isinstance(value, str) and len(value) > 128:
            raise PolicyError("oversized_observation")

    def _apply(self, key, value, source, now) -> Change:
        previous = self.fields.get(key)
        if (
            previous
            and previous.generation == self.generation
            and now < previous.received_monotonic
        ):
            return Change(False, False, "older_local_receipt")
        notify = previous is None or (
            previous.value,
            previous.source,
            previous.generation,
        ) != (value, source, self.generation)
        self._capacity(self.fields, key, "field_limit")
        self.fields[key] = Observation(value, source, now, self.generation)
        self.notifications += notify
        return Change(True, notify, "observation_updated")

    def accept_mqtt(
        self,
        group: str,
        peer: str,
        field: str,
        value: float | str,
        *,
        retained: bool | None,
        now: float,
        generation: int,
    ) -> Change:
        key, now = self._key(group, peer, field), self._clock(now)
        self._value(value)
        if not self.active or generation != self.generation:
            return Change(False, False, "obsolete_capture_generation")
        if retained is not False:
            previous = self.caches.get(key)
            if previous and previous.generation == generation and now < previous.received_monotonic:
                return Change(False, False, "older_cache_receipt")
            self._capacity(self.caches, key, "cache_limit")
            source = SourceKind.RETAINED_CACHE if retained is True else SourceKind.UNKNOWN_RETAIN
            self.caches[key] = Observation(value, source, now, generation)
            return Change(True, False, "cache_only")
        result = self._apply(key, value, SourceKind.DIRECT_MQTT, now)
        if result.applied:
            self.revision += 1
            self.direct_revisions[key] = self.revision
        return result

    def begin_query(
        self,
        group: str,
        *,
        now: float,
        subscriptions_ready: bool,
        timeout: float = 10,
        minimum_interval: float = 60,
    ) -> QueryToken:
        group, now = identifier(group, physical=False), self._clock(now)
        if not self.active or group not in self.bindings.accepted_groups:
            raise PolicyError("query_group_capture_not_ready")
        if subscriptions_ready is not True:
            raise PolicyError("actual_subscription_readiness_required")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 60
            or isinstance(minimum_interval, bool)
            or not isinstance(minimum_interval, (int, float))
            or minimum_interval < 0
            or not math.isfinite(minimum_interval)
        ):
            raise PolicyError("invalid_query_limits")
        outstanding = self.queries.get(group)
        if outstanding and now <= outstanding.expires:
            raise PolicyError("query_already_outstanding")
        if now - self.last_query.get(group, -math.inf) < minimum_interval:
            raise PolicyError("query_rate_limited")
        self._serial += 1
        token = QueryToken(
            group,
            self.generation,
            self._serial,
            self.revision,
            now,
            now + timeout,
            owner=self._query_owner,
            broker_scope=self.bindings.broker_scope,
        )
        self.queries[group], self.last_query[group] = token, now
        return token

    def accept_root_cache(
        self,
        token: QueryToken,
        peer: str,
        field: str,
        value: float | str,
        *,
        retained: bool | None,
        now: float,
    ) -> Change:
        key, now = self._key(token.group, peer, field), self._clock(now)
        self._value(value)
        if retained is not False:
            return Change(False, False, "query_reply_not_known_nonretained")
        if (
            not self.active
            or token.generation != self.generation
            or token.owner is not self._query_owner
            or token.broker_scope != self.bindings.broker_scope
            or self.queries.get(token.group) is not token
        ):
            return Change(False, False, "query_generation_or_window_not_active")
        if not token.started <= now <= token.expires:
            return Change(False, False, "query_reply_outside_window")
        previous_cache = self.root_caches.get(key)
        if (
            previous_cache
            and previous_cache.generation == self.generation
            and (now < previous_cache.received_monotonic)
        ):
            return Change(False, False, "older_root_cache_receipt")
        self._capacity(self.root_caches, key, "root_cache_limit")
        self.root_caches[key] = Observation(value, SourceKind.ROOT_CACHE, now, self.generation)
        self._root_cache_start_revisions[key] = token.start_revision
        if self.direct_revisions.get(key, 0) > token.start_revision:
            return Change(False, False, "newer_direct_report_wins")
        previous = self.fields.get(key)
        if (
            previous
            and previous.generation == self.generation
            and previous.source in {SourceKind.DIRECT_MQTT, SourceKind.DIRECT_UDP}
        ):
            if previous.value == value and self._fresh(previous, now):
                return Change(False, False, "matching_fresh_direct_report_preferred")
        return self._apply(key, value, SourceKind.ROOT_CACHE, now)

    def accept_udp(
        self, notice: UdpNotice, source_ip: str, *, now: float, generation: int
    ) -> Change:
        group, peer = self.bindings.route_udp(notice, source_ip)
        now = self._clock(now)
        if not self.active or generation != self.generation:
            return Change(False, False, "obsolete_capture_generation")
        duplicate = (
            notice.root_mac,
            peer,
            notice.flag_raw,
            notice.payload_sha256,
            generation,
        )
        for key, received in tuple(self._seen_udp.items()):
            if now - received > self.udp_duplicate_window:
                del self._seen_udp[key]
        if duplicate in self._seen_udp:
            return Change(False, False, "duplicate_udp_notice")
        key = self._key(group, peer, "output_raw")
        result = self._apply(key, notice.output_raw, SourceKind.DIRECT_UDP, now)
        if not result.applied:
            return result
        self._seen_udp[duplicate] = now
        while len(self._seen_udp) > 256:
            self._seen_udp.popitem(last=False)
        self.revision += 1
        self.direct_revisions[key] = self.revision
        return result

    def begin_provisional_query(self, group: str, **kwargs: Any) -> QueryToken:
        """Same bounded window for user-qualified configured groups without fresh members."""
        return self.begin_query(group, **kwargs)

    def current(
        self, group: str, peer: str, field: str, *, now: float | None = None
    ) -> Observation | None:
        """Return a leased reported observation; never reinterpret receipt as peer age."""
        key = self._key(group, peer, field)
        checked_now = self._clock(self.clock() if now is None else now)
        observation = self.fields.get(key)
        if observation is not None and self._fresh(observation, checked_now):
            return observation
        cache = self.root_caches.get(key)
        if (
            cache is not None
            and self._fresh(cache, checked_now)
            and self.direct_revisions.get(key, 0) <= self._root_cache_start_revisions.get(key, -1)
        ):
            return cache
        return None
