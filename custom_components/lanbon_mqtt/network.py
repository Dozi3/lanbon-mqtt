"""Optional, bounded root metadata reads through HA's shared HTTP session.

Only explicit numeric private root bindings are accepted. This module has no
MQTT client, discovery probe, status getter or actuator endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import UTC, datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from yarl import URL

from .policy import identifier

MAX_ROOTS = 16
MAX_CHILDREN_PER_CYCLE = 16
MAX_CHILD_CANDIDATES = 256
CHILD_REQUEST_GAP = 0.25
MAX_RESPONSE_BYTES = 8192
FIRST_POLL_DELAY = 15
POLL_INTERVAL = 300
REQUEST_TIMEOUT = 5
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _private_host(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 45 or "%" in value:
        raise ValueError("invalid_root_host")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("invalid_root_host") from error
    if (
        address.version != 4
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or not any(address.version == net.version and address in net for net in _PRIVATE_NETWORKS)
    ):
        raise ValueError("invalid_root_host")
    return str(address)


def normalize_root_bindings(value: Any, expected_groups: Any) -> dict[str, dict[str, str]]:
    """Validate configuration without accepting URLs, hostnames or extra fields."""
    if not isinstance(expected_groups, (list, tuple, set, frozenset)):
        raise ValueError("invalid_root_groups")
    if len(expected_groups) > MAX_ROOTS:
        raise ValueError("invalid_root_groups")
    try:
        accepted = {identifier(group, physical=False) for group in expected_groups}
    except ValueError as error:
        raise ValueError("invalid_root_groups") from error
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ROOTS:
        raise ValueError("invalid_root_bindings")
    result: dict[str, dict[str, str]] = {}
    hosts: set[str] = set()
    roots: set[str] = set()
    for key, binding in value.items():
        try:
            group = identifier(key, physical=False)
            if group not in accepted or group in result:
                raise ValueError("invalid_root_group")
            if not isinstance(binding, dict) or set(binding) != {"host", "root_mac"}:
                raise ValueError("invalid_root_binding")
            host = _private_host(binding["host"])
            root = identifier(binding["root_mac"])
            if host in hosts or root in roots:
                raise ValueError("duplicate_root_binding")
        except (ValueError, TypeError) as error:
            raise ValueError("invalid_root_bindings") from error
        result[group] = {"host": host, "root_mac": root}
        hosts.add(host)
        roots.add(root)
    return result


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("nonfinite_json_value")


@callback
def _shared_session(hass: HomeAssistant) -> aiohttp.ClientSession:
    # Load the shared HTTP/mDNS dependencies only when optional polling starts.
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    return async_get_clientsession(hass)


class RootMetadataPoller:
    """One delayed task reads roots sequentially, with no overlapping cycles."""

    def __init__(
        self, hass: HomeAssistant, hub: Any, bindings: Any, *, include_children: bool = False
    ) -> None:
        if type(include_children) is not bool:
            raise ValueError("invalid_metadata_children")
        self.hass, self.hub = hass, hub
        self.bindings = normalize_root_bindings(bindings, hub.groups)
        self.include_children = include_children
        self.status: dict[str, str] = {group: "not_started" for group in self.bindings}
        self.child_attempted = 0
        self.child_accepted = 0
        self._child_cursor = 0
        self._running = False
        self._timer: asyncio.TimerHandle | None = None
        self._task: asyncio.Task | None = None
        self._cycle_started = 0.0

    @callback
    def async_start(self) -> None:
        """Schedule without delaying config entry setup or opening any connection."""
        if self._running or not self.bindings:
            return
        self._running = True
        self._timer = self.hass.loop.call_later(FIRST_POLL_DELAY, self._launch)

    @callback
    def async_stop(self) -> None:
        """Cancel owned work; the shared HA session is not owned by this poller."""
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @callback
    def _launch(self) -> None:
        self._timer = None
        if not self._running or self._task is not None:
            return
        self._cycle_started = self.hass.loop.time()
        self._task = self.hass.async_create_background_task(
            self._async_poll(), "Lanbon optional root metadata", eager_start=False
        )
        self._task.add_done_callback(self._finished)

    @callback
    def _finished(self, task: asyncio.Task) -> None:
        error = None if task.cancelled() else task.exception()
        if task is not self._task:
            return
        self._task = None
        if error is not None:
            # Never log a raw HTTP exception or a returned document.
            for group in self.bindings:
                self.status[group] = "internal_error"
        if self._running:
            delay = max(0, POLL_INTERVAL - (self.hass.loop.time() - self._cycle_started))
            self._timer = self.hass.loop.call_later(delay, self._launch)

    async def _async_request(
        self, session, binding, *, device_info: bool, target_mac: str | None = None
    ) -> dict:
        if target_mac is not None and not device_info:
            raise ValueError("metadata_target_requires_device_info")
        host = binding["host"]
        path = "/device_request" if device_info else "/mesh_info"
        headers = {"Connection": "close"}
        kwargs = {}
        if device_info:
            target = identifier(target_mac or binding["root_mac"])
            headers.update({"Content-Type": "application/json", "Mesh-Node-Mac": target.lower()})
            kwargs["json"] = {"request": "get_device_info"}
        async with session.request(
            "POST" if device_info else "GET",
            URL.build(scheme="http", host=host, port=80, path=path),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            allow_redirects=False,
            **kwargs,
        ) as response:
            if response.status != 200:
                raise ValueError("metadata_http_status")
            if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                raise ValueError("metadata_body_limit")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(4096):
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError("metadata_body_limit")
            body = json.loads(
                bytes(raw), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
            )
            if not isinstance(body, dict):
                raise ValueError("metadata_json_not_object")
            selected_headers = {}
            for key in ("content-type", "mesh-node-mac", "mesh-node-num"):
                values = response.headers.getall(key, [])
                if len(values) > 1:
                    raise ValueError("metadata_duplicate_header")
                if values:
                    selected_headers[key] = values[0]
            return {
                "http_status": response.status,
                "request_host": host,
                "headers": selected_headers,
                "body": body,
            }

    async def _async_poll(self) -> None:
        from .metadata import parse_metadata

        session = _shared_session(self.hass)
        self.child_attempted = self.child_accepted = 0
        contexts = {}
        for group, binding in self.bindings.items():
            if not self._running:
                return
            try:
                mesh = await self._async_request(session, binding, device_info=False)
                device = await self._async_request(session, binding, device_info=True)
                normalized = parse_metadata(
                    mesh,
                    device,
                    root_ip=binding["host"],
                    observed_at=datetime.now(UTC).isoformat(),
                    expected_groups={group},
                    expected_root_mac=binding["root_mac"],
                )
                accepted = self.hub.async_accept_network_metadata(normalized)
                self.status[group] = "accepted" if accepted is True else "mqtt_owner_not_current"
                if accepted is True and self.include_children:
                    contexts[group] = (binding, mesh, device, normalized)
            except aiohttp.ClientError, TimeoutError, OSError:
                self.status[group] = "metadata_unavailable"
            except ValueError, TypeError, KeyError, RecursionError:
                self.status[group] = "invalid_metadata"
        if self.include_children and self._running:
            await self._async_poll_children(session, contexts)

    def _eligible_children(self, group, normalized) -> set[str]:
        """HTTP membership only filters MQTT-qualified peers; it cannot create them."""
        peers = self.hub.async_network_metadata_peers(group)
        if (
            not isinstance(peers, (tuple, list, set, frozenset))
            or len(peers) > MAX_CHILD_CANDIDATES
        ):
            return set()
        members = set(normalized["fields"]["mesh_members"])
        root = normalized["root_mac"]
        return {identifier(peer) for peer in peers} & (members - {root})

    async def _async_poll_children(self, session, contexts) -> None:
        from .metadata import parse_child_metadata

        candidates = []
        for group in sorted(contexts):
            normalized = contexts[group][3]
            for peer in sorted(self._eligible_children(group, normalized)):
                if len(candidates) >= MAX_CHILD_CANDIDATES:
                    break
                candidates.append((group, peer))
        if not candidates:
            self._child_cursor = 0
            return
        start = self._child_cursor % len(candidates)
        selected = (candidates[start:] + candidates[:start])[:MAX_CHILDREN_PER_CYCLE]
        self._child_cursor = (start + len(selected)) % len(candidates)
        # A single bounded cursor supplies fairness across both roots and peers.
        for group, peer in selected:
            if not self._running:
                return
            binding, mesh, root_info, normalized = contexts[group]
            await asyncio.sleep(CHILD_REQUEST_GAP)
            if not self._running:
                return
            # Recheck current broker generation/membership after every await.
            if peer not in self._eligible_children(group, normalized):
                continue
            self.child_attempted += 1
            try:
                child_info = await self._async_request(
                    session, binding, device_info=True, target_mac=peer
                )
                child = parse_child_metadata(
                    mesh,
                    root_info,
                    child_info,
                    root_ip=binding["host"],
                    expected_root_mac=binding["root_mac"],
                    expected_peer_mac=peer,
                    expected_groups={group},
                    observed_at=datetime.now(UTC).isoformat(),
                )
                accepted = self.hub.async_accept_network_metadata(child)
                if accepted is True:
                    self.child_accepted += 1
            except (
                aiohttp.ClientError,
                TimeoutError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                RecursionError,
            ):
                # A failed child neither retries nor suppresses remaining peers.
                accepted = False
            if accepted is not True:
                self.status[group] = "accepted_children_partial"
