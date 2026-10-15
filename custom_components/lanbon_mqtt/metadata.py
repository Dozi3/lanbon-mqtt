"""Pure, bounded metadata normalization; no HTTP, UDP or command transport."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

MAX_BODY_BYTES = 16384
MAX_MESH_PEERS = 256
MAX_CHARACTERISTICS = 32
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
UDP_STATUS = re.compile(
    rb"mac=([0-9a-fA-F]{12})\r\nflag=(-?[0-9]{1,10})\r\n"
    rb"type=status\[([0-9a-fA-F]{12})_([0-9]{1,3})\]\r\n\Z"
)


def _mac(value, *, physical=True):
    if not isinstance(value, str):
        raise ValueError("Expected hexadecimal identity")
    if not re.fullmatch(r"[0-9A-Fa-f]{12}|(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", value):
        raise ValueError("Invalid hexadecimal identity")
    normalized = value.replace(":", "")
    if not re.fullmatch(r"[0-9A-Fa-f]{12}", normalized):
        raise ValueError("Invalid hexadecimal identity")
    raw = bytes.fromhex(normalized)
    if not any(raw) or raw == b"\xff" * 6 or (physical and raw[0] & 1):
        raise ValueError("Invalid scoped identity")
    return normalized.upper()


def _ip(value):
    if not isinstance(value, str) or len(value) > 15 or value != value.strip():
        raise ValueError("Expected private IPv4 literal")
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise ValueError("Expected private IPv4 literal") from error
    if not any(address in network for network in PRIVATE_NETWORKS):
        raise ValueError("Address is outside the qualified private IPv4 profile")
    return str(address)


def _text(value, limit=128, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not value and not empty):
        raise ValueError("Invalid bounded metadata string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Control characters in metadata")
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid bounded integer")
    return value


def _http(envelope, root_ip):
    if not isinstance(envelope, Mapping) or envelope.get("http_status") != 200:
        raise ValueError("HTTP metadata request did not succeed")
    if type(envelope["http_status"]) is not int or _ip(envelope.get("request_host")) != root_ip:
        raise ValueError("Metadata request target mismatch")
    body, headers = envelope.get("body"), envelope.get("headers")
    if not isinstance(body, dict) or not isinstance(headers, Mapping):
        raise ValueError("Expected bounded HTTP metadata envelope")
    try:
        encoded = json.dumps(body, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("Invalid JSON metadata") from error
    if len(encoded) > MAX_BODY_BYTES or len(headers) > 32:
        raise ValueError("HTTP metadata exceeds bound")
    normalized = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Invalid HTTP header")
        if len(key) > 64 or len(value) > 4096:
            raise ValueError("HTTP header exceeds bound")
        lower = key.lower()
        if lower in normalized:
            raise ValueError("Ambiguous repeated metadata header")
        normalized[lower] = value
    if type(body.get("status_code")) is not int or body["status_code"] != 0:
        raise ValueError("Device metadata reported failure")
    return body, normalized


def _members(header):
    if not isinstance(header, str) or not header or len(header) > 4096:
        raise ValueError("Missing bounded mesh membership")
    peers = []
    for token in header.split(","):
        value = token.strip()
        # This MDF header formatter has been observed dropping a leading zero.
        # Accept exactly the witnessed 11/12-hex shapes, never arbitrary padding.
        if re.fullmatch(r"[0-9a-fA-F]{11}", value):
            value = "0" + value
        peer = _mac(value)
        if peer in peers:
            raise ValueError("Duplicate mesh member")
        peers.append(peer)
    if not 1 <= len(peers) <= MAX_MESH_PEERS:
        raise ValueError("Mesh membership exceeds bound")
    return peers


def _characteristics(value):
    if not isinstance(value, list) or len(value) > MAX_CHARACTERISTICS:
        raise ValueError("Invalid characteristic collection")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Invalid characteristic")
        cid = _integer(item.get("cid"), 0, 65535)
        if cid in seen:
            raise ValueError("Duplicate characteristic")
        seen.add(cid)
        row = {"cid": cid}
        for key in ("name", "format"):
            if key in item:
                row[key] = _text(item[key], 64)
        if "perms" in item:
            row["perms"] = _integer(item["perms"], 0, 255)
        for key in ("value", "min", "max", "step"):
            if key not in item:
                continue
            raw = item[key]
            if isinstance(raw, str):
                row[key] = _text(raw, 128, empty=True)
            elif raw is None or type(raw) is bool:
                row[key] = raw
            elif type(raw) in (int, float) and abs(raw) <= 2**53 and math.isfinite(raw):
                row[key] = raw
            else:
                raise ValueError("Unsupported characteristic value")
        result.append(row)
    return result


def _observed(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Invalid metadata receipt timestamp") from error
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Metadata receipt time must be timezone aware")
    return value.astimezone(UTC).isoformat()


def _device_fields(body, *, peer, group, layer, address, address_key, members=None):
    rssi = _integer(body.get("rssi"), -120, 10)
    fields = {
        "self_mac": peer,
        "parent_mac": _mac(body.get("parent_mac")),
        "mesh_id": group,
        address_key: address,
        "layer": layer,
        "rssi_raw": rssi,
        "rssi": None if rssi in {-120, 0} else rssi,
        "rssi_fallback": rssi in {-120, 0},
        "version_namespace": "mlink_application_not_lanbon_firmware",
        "characteristic_state_qualified": False,
        "characteristics": _characteristics(body.get("characteristics", [])),
        "status_code": 0,
    }
    if members is not None:
        fields.update(mesh_node_count=len(members), mesh_members=members)
    for key in ("name", "version", "idf_version", "mdf_version", "status_msg", "project_name"):
        if key in body:
            fields[key] = _text(body[key], empty=key == "mdf_version")
    for key in ("mlink_version", "mlink_trigger"):
        if key in body:
            fields[key] = _integer(body[key], 0, 65535)
    if "tsf_time" in body:
        tsf = body["tsf_time"]
        if type(tsf) is int:
            tsf = str(tsf)
        if not isinstance(tsf, str) or not re.fullmatch(r"-?[0-9]{1,20}", tsf):
            raise ValueError("Invalid raw TSF value")
        if not -(2**63) <= int(tsf) < 2**63:
            raise ValueError("Raw TSF exceeds source integer bound")
        fields["tsf_time_raw"] = tsf
    if "tid" in body:
        fields["tid"] = _text(body["tid"], 64)
    return fields


def parse_metadata(
    mesh_info,
    device_info,
    *,
    root_ip: str,
    expected_root_mac: str,
    expected_groups: Iterable[str],
    observed_at: datetime | str | None = None,
) -> dict:
    """Normalize verified root metadata; all invalid evidence raises ValueError.

    Callers must independently establish the expected root's current MQTT group
    ownership. HTTP fields never qualify a Lanbon model, output or actuator.
    """
    ip = _ip(root_ip)
    root = _mac(expected_root_mac)
    if isinstance(expected_groups, (str, bytes)):
        raise ValueError("Expected a collection of accepted groups")
    try:
        groups = set()
        for index, value in enumerate(expected_groups):
            if index >= 16:
                raise ValueError("Accepted groups exceed bound")
            groups.add(_mac(value, physical=False))
    except TypeError as error:
        raise ValueError("Invalid accepted groups") from error
    if not 1 <= len(groups) <= 16:
        raise ValueError("Accepted groups exceed bound")
    _, mesh_headers = _http(mesh_info, ip)
    body, device_headers = _http(device_info, ip)
    members = _members(mesh_headers.get("mesh-node-mac"))
    raw_count = mesh_headers.get("mesh-node-num")
    if not isinstance(raw_count, str) or not re.fullmatch(r"[0-9]{1,3}", raw_count):
        raise ValueError("Invalid mesh member count")
    if int(raw_count) != len(members):
        raise ValueError("Mesh member count mismatch")
    if root not in members or _members(device_headers.get("mesh-node-mac")) != [root]:
        raise ValueError("HTTP root membership/target mismatch")
    if _mac(body.get("self_mac")) != root:
        raise ValueError("HTTP self identity mismatch")
    group = _mac(body.get("mesh_id"), physical=False)
    if group not in groups:
        raise ValueError("HTTP group is not accepted")
    if _integer(body.get("layer"), 1, 25) != 1:
        raise ValueError("Root-only profile rejects child metadata")
    fields = _device_fields(
        body, peer=root, group=group, layer=1, address=ip, address_key="ip_address", members=members
    )
    result = {
        "group_id": group,
        "device_id": root,
        "root_mac": root,
        "fields": fields,
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
        "root_identity_basis": "expected_self_mac_and_mesh_header_membership",
        "physical_state_qualified": False,
    }
    timestamp = _observed(observed_at)
    if timestamp is not None:
        result["observed_at"] = timestamp
    return result


def parse_child_metadata(
    mesh_info,
    root_info,
    child_info,
    *,
    root_ip: str,
    expected_root_mac: str,
    expected_peer_mac: str,
    expected_groups: Iterable[str],
    observed_at: datetime | str | None = None,
) -> dict:
    """Normalize a targeted child reply only after strict root proof succeeds."""
    verified_root = parse_metadata(
        mesh_info,
        root_info,
        root_ip=root_ip,
        expected_root_mac=expected_root_mac,
        expected_groups=expected_groups,
        observed_at=observed_at,
    )
    root, peer = verified_root["device_id"], _mac(expected_peer_mac)
    if peer == root or peer not in verified_root["fields"]["mesh_members"]:
        raise ValueError("Child is not a distinct verified mesh member")
    ip = verified_root["fields"]["ip_address"]
    body, headers = _http(child_info, ip)
    if _members(headers.get("mesh-node-mac")) != [peer] or _mac(body.get("self_mac")) != peer:
        raise ValueError("Child response target/self mismatch")
    group = _mac(body.get("mesh_id"), physical=False)
    if group != verified_root["group_id"]:
        raise ValueError("Child response group differs from verified root")
    layer = _integer(body.get("layer"), 2, 25)
    fields = _device_fields(
        body, peer=peer, group=group, layer=layer, address=ip, address_key="gateway_address"
    )
    fields["gateway_mesh_node_count"] = verified_root["fields"]["mesh_node_count"]
    result = {
        "group_id": group,
        "device_id": peer,
        "root_mac": root,
        "role": "child",
        "fields": fields,
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
        "root_identity_basis": verified_root["root_identity_basis"],
        "peer_identity_basis": "expected_self_mac_and_verified_root_membership",
        "physical_state_qualified": False,
    }
    if "observed_at" in verified_root:
        result["observed_at"] = verified_root["observed_at"]
    return result


def parse_udp_notice(payload: bytes, *, expected_root_mac: str | None = None) -> dict | None:
    """Decode one unauthenticated notice; no group/type/state inference."""
    if not isinstance(payload, bytes) or len(payload) > 128:
        return None
    match = UDP_STATUS.fullmatch(payload)
    if match is None:
        return None
    try:
        root = _mac(match[1].decode("ascii"))
        peer = _mac(match[3].decode("ascii"))
        if expected_root_mac is not None and root != _mac(expected_root_mac):
            return None
        flag, output = int(match[2]), int(match[4])
        if not -(2**31) <= flag <= 2**32 - 1 or output > 255:
            return None
    except ValueError:
        return None
    return {
        "device_id": peer,
        "root_mac": root,
        "source": "unauthenticated_udp_notice",
        "profile": "mlink_notice_status_v1",
        "fields": {
            "notice_type": "status",
            "output_raw": output,
            "flag_raw": match[2].decode("ascii"),
            "flag_u32": flag & 0xFFFFFFFF,
            "flag_semantics": "source_matched_rtos_ticks_not_utc_or_proven_uptime",
        },
        "dedup_key": (root, match[2].decode("ascii"), peer, output),
        "authenticated": False,
        "physical_state_qualified": False,
    }
