"""Root binding and fixed HTTP lifecycle tests with synthetic loopback services."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant

from custom_components.lanbon_mqtt import network

GROUP = "102233445566"
ROOT = "020000000002"
PEER = "020000000004"
HOST = "10.12.34.56"
BINDINGS = {GROUP: {"host": HOST, "root_mac": ROOT}}


def device_info():
    return {
        "tid": "1",
        "name": "synthetic_root",
        "self_mac": ROOT.lower(),
        "parent_mac": PEER.lower(),
        "mesh_id": GROUP.lower(),
        "version": "1.1.0",
        "idf_version": "v3.3.5-dirty",
        "mdf_version": "",
        "mlink_version": 2,
        "mlink_trigger": 0,
        "rssi": -51,
        "layer": 1,
        "tsf_time": "123456789",
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
        "status_code": 0,
        "status_msg": "MDF_OK",
    }


def test_empty_binding_and_canonicalization_are_explicit():
    assert network.normalize_root_bindings(None, [GROUP]) == {}
    assert network.normalize_root_bindings({}, [GROUP]) == {}
    bindings = {"10:22:33:44:55:66": {"host": HOST, "root_mac": "02:00:00:00:00:02"}}
    assert network.normalize_root_bindings(bindings, [GROUP]) == BINDINGS
    assert bindings["10:22:33:44:55:66"]["root_mac"] == "02:00:00:00:00:02"


@pytest.mark.parametrize(
    "host",
    [
        "https://10.1.2.3",
        "10.1.2.3:80",
        "root@10.1.2.3",
        "example.local",
        "127.0.0.1",
        "169.254.1.1",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "8.8.8.8",
        "100.64.0.1",
        "192.0.2.1",
        "10.1.2.3/path",
        "010.1.2.3",
        " 10.1.2.3",
        "10.1.2.3\n",
        "::1",
        "fe80::1%eth0",
        "fd00::1",
        "::ffff:10.1.2.3",
        "[fd00::1]",
        True,
        None,
    ],
)
def test_root_binding_rejects_unsupported_or_expanding_hosts(host):
    with pytest.raises(ValueError, match="invalid_root_bindings"):
        network.normalize_root_bindings({GROUP: {"host": host, "root_mac": ROOT}}, [GROUP])


@pytest.mark.parametrize("root", ["000000000000", "FFFFFFFFFFFF", "010000000002", "bad", None])
def test_invalid_root_identity_is_rejected(root):
    with pytest.raises(ValueError, match="invalid_root_bindings"):
        network.normalize_root_bindings({GROUP: {"host": HOST, "root_mac": root}}, [GROUP])


def test_root_scope_shape_duplicates_and_limits_are_fail_closed():
    for value in [
        [],
        "",
        {GROUP: {"host": HOST, "root_mac": ROOT, "password": "unused"}},
        {GROUP: {"host": HOST}},
        {PEER: {"host": HOST, "root_mac": ROOT}},
        {GROUP: {"host": HOST, "root_mac": ROOT}, GROUP.lower(): {"host": HOST, "root_mac": PEER}},
    ]:
        # GROUP contains no letters; the final literal is a duplicate dict key,
        # so test normalized duplicates explicitly below instead.
        if value == {GROUP: {"host": HOST, "root_mac": PEER}}:
            continue
        with pytest.raises(ValueError):
            network.normalize_root_bindings(value, [GROUP])
    other = "202233445566"
    for second in ({"host": HOST, "root_mac": PEER}, {"host": "10.12.34.57", "root_mac": ROOT}):
        with pytest.raises(ValueError):
            network.normalize_root_bindings({**BINDINGS, other: second}, [GROUP, other])
    with pytest.raises(ValueError):
        network.normalize_root_bindings(
            {GROUP: BINDINGS[GROUP], "10:22:33:44:55:66": BINDINGS[GROUP]}, [GROUP]
        )
    with pytest.raises(ValueError):
        network.normalize_root_bindings({}, "not-a-group-list")
    with pytest.raises(ValueError):
        network.normalize_root_bindings({}, [f"02000000{i:04X}" for i in range(17)])


@pytest.fixture
async def http_root(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    state = SimpleNamespace(
        calls=[],
        requests=[],
        overrides={},
        gate=None,
        entered=asyncio.Event(),
        members=[ROOT, PEER],
    )

    async def handler(request):
        state.requests.append(
            (request.method, request.path, dict(request.headers), await request.read())
        )
        state.entered.set()
        if state.gate is not None:
            await state.gate.wait()
        if override := state.overrides.get((request.method, request.path)):
            response = override(request)
            return await response if inspect.isawaitable(response) else response
        if request.path == "/mesh_info" and request.method == "GET":
            return web.json_response(
                {"status_code": 0},
                headers={
                    "Mesh-Node-Mac": ",".join(state.members).lower(),
                    "Mesh-Node-Num": str(len(state.members)),
                },
            )
        if request.path == "/device_request" and request.method == "POST":
            target = request.headers["Mesh-Node-Mac"].upper()
            body = device_info()
            if target != ROOT:
                body.update(self_mac=target, parent_mac=ROOT, layer=2)
            return web.json_response(body, headers={"Mesh-Node-Mac": target.lower()})
        raise AssertionError("A non-metadata endpoint was requested")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    # Numeric loopback transport needs no platform-specific async DNS backend.
    session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
    )

    class TestTransport:
        def request(self, method, url, **kwargs):
            # Verify the production URL BEFORE redirecting only the test transport
            # to this isolated, ephemeral loopback service.
            assert url.host == HOST and url.port == 80 and url.scheme == "http"
            assert url.path in {"/mesh_info", "/device_request"}
            state.calls.append((method, url, kwargs))
            return session.request(method, origin + url.path, **kwargs)

    try:
        with patch.object(network, "_shared_session", return_value=TestTransport()):
            yield hass, state, session
    finally:
        if state.gate is not None:
            state.gate.set()
        await session.close()
        await runner.cleanup()
        await hass.async_stop(force=True)


def make_hub():
    return SimpleNamespace(groups=[GROUP], async_accept_network_metadata=Mock(return_value=True))


async def poll_once(hass, hub):
    poller = network.RootMetadataPoller(hass, hub, BINDINGS)
    poller._running = True
    await poller._async_poll()
    poller.async_stop()
    return poller


async def test_real_http_uses_only_two_fixed_reads_and_normalized_callback(http_root):
    hass, state, _ = http_root
    hub = make_hub()
    poller = await poll_once(hass, hub)
    assert poller.status == {GROUP: "accepted"}
    assert [(row[0], row[1].path) for row in state.calls] == [
        ("GET", "/mesh_info"),
        ("POST", "/device_request"),
    ]
    for _, _, kwargs in state.calls:
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"].total == 5
        assert kwargs["headers"]["Connection"] == "close"
        assert "Root-Response" not in kwargs["headers"]
    post = state.calls[1][2]
    assert post["json"] == {"request": "get_device_info"}
    assert post["headers"]["Mesh-Node-Mac"] == ROOT.lower()
    normalized = hub.async_accept_network_metadata.call_args.args[0]
    assert normalized["group_id"] == GROUP
    assert normalized["root_mac"] == ROOT
    assert "power" not in normalized and "brightness" not in normalized


@pytest.mark.parametrize(
    "failure", ["redirect", "oversize", "invalid_json", "array", "duplicate", "nonfinite"]
)
async def test_bad_first_response_never_calls_second_endpoint_or_ingress(http_root, failure):
    hass, state, _ = http_root
    bodies = {
        "oversize": b"x" * 8193,
        "invalid_json": b"{",
        "array": b"[]",
        "duplicate": b'{"status_code":0,"status_code":0}',
        "nonfinite": b'{"value":NaN}',
    }
    state.overrides[("GET", "/mesh_info")] = (
        (lambda request: web.Response(status=302, headers={"Location": "http://8.8.8.8/forbidden"}))
        if failure == "redirect"
        else lambda request: web.Response(body=bodies[failure])
    )
    hub = make_hub()
    poller = await poll_once(hass, hub)
    assert poller.status == {GROUP: "invalid_metadata"}
    assert len(state.calls) == 1
    hub.async_accept_network_metadata.assert_not_called()


async def test_valid_http_still_requires_current_mqtt_owner_callback(http_root):
    hass, _, _ = http_root
    hub = make_hub()
    hub.async_accept_network_metadata.return_value = False
    poller = await poll_once(hass, hub)
    assert poller.status == {GROUP: "mqtt_owner_not_current"}


async def test_poll_schedule_is_nonblocking_single_task_and_stop_cancels(http_root):
    hass, state, session = http_root
    hub = make_hub()
    poller = network.RootMetadataPoller(hass, hub, BINDINGS)
    poller.async_start()
    first = poller._timer
    assert first is not None and not state.calls and poller._task is None
    assert 14.9 <= first.when() - hass.loop.time() <= 15
    poller.async_start()
    assert poller._timer is first
    first.cancel()
    state.gate = asyncio.Event()
    poller._launch()
    task = poller._task
    await asyncio.wait_for(state.entered.wait(), 1)
    poller._launch()
    assert poller._task is task and len(state.calls) == 1
    poller.async_stop()
    with pytest.raises(asyncio.CancelledError):
        await task
    state.gate.set()
    assert poller._timer is None and poller._task is None and not session.closed
    hub.async_accept_network_metadata.assert_not_called()


async def test_completed_cycle_schedules_300_seconds_from_start_without_overlap(http_root):
    hass, _, _ = http_root
    poller = network.RootMetadataPoller(hass, make_hub(), BINDINGS)
    poller._running = True
    poller._launch()
    started = poller._cycle_started
    await poller._task
    await asyncio.sleep(0)
    assert poller._task is None and poller._timer is not None
    assert abs(poller._timer.when() - started - 300) < 0.05
    poller.async_stop()


async def test_no_binding_schedules_no_http_or_task(http_root):
    hass, state, _ = http_root
    poller = network.RootMetadataPoller(hass, make_hub(), {})
    poller.async_start()
    assert poller._timer is None and poller._task is None and not state.calls
    poller.async_stop()


async def test_http_timeout_is_sanitized_without_callback(http_root):
    hass, state, _ = http_root
    state.gate = asyncio.Event()
    hub = make_hub()
    with patch.object(network, "REQUEST_TIMEOUT", 0.02):
        poller = await poll_once(hass, hub)
    state.gate.set()
    assert poller.status == {GROUP: "metadata_unavailable"}
    hub.async_accept_network_metadata.assert_not_called()


async def test_chunked_body_is_bounded_without_a_content_length(http_root):
    hass, state, _ = http_root

    async def chunked(request):
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        await response.write(b" " * 4096)
        await response.write(b" " * 4096)
        await response.write(b" ")
        await response.write_eof()
        return response

    state.overrides[("GET", "/mesh_info")] = chunked
    hub = make_hub()
    poller = await poll_once(hass, hub)
    assert poller.status == {GROUP: "invalid_metadata"}
    assert len(state.calls) == 1
    hub.async_accept_network_metadata.assert_not_called()


def test_exact_sixteen_root_boundaries_are_supported():
    groups = [f"02000000{i:04X}" for i in range(1, 17)]
    bindings = {
        group: {"host": f"10.1.2.{i}", "root_mac": f"02010000{i:04X}"}
        for i, group in enumerate(groups, 1)
    }
    assert len(network.normalize_root_bindings(bindings, groups)) == 16


async def test_children_are_opt_in_and_two_real_http_targets_keep_gateway_provenance(http_root):
    hass, state, _ = http_root
    peer2 = "020000000006"
    state.members.append(peer2)
    hub = make_hub()
    hub.async_network_metadata_peers = Mock(return_value=(ROOT, PEER, peer2, "020000000008"))
    # Even a helper full of eligible peers does nothing without the explicit flag.
    await poll_once(hass, hub)
    hub.async_network_metadata_peers.assert_not_called()
    state.calls.clear()
    hub.async_accept_network_metadata.reset_mock()
    poller = network.RootMetadataPoller(hass, hub, BINDINGS, include_children=True)
    poller._running = True
    await poller._async_poll()
    poller.async_stop()
    assert len(state.calls) == 4
    targets = [kwargs["headers"].get("Mesh-Node-Mac") for _, _, kwargs in state.calls]
    assert targets == [None, ROOT.lower(), PEER.lower(), peer2.lower()]
    for _, url, kwargs in state.calls:
        assert url.host == HOST and url.port == 80 and not kwargs["allow_redirects"]
    accepted = [call.args[0] for call in hub.async_accept_network_metadata.call_args_list]
    assert [item["device_id"] for item in accepted] == [ROOT, PEER, peer2]
    for child in accepted[1:]:
        assert child["role"] == "child" and child["root_mac"] == ROOT
        assert child["fields"]["gateway_address"] == HOST
        assert "ip_address" not in child["fields"]
        assert child["fields"]["layer"] == 2
        assert child["physical_state_qualified"] is False
    assert (poller.child_attempted, poller.child_accepted) == (2, 2)


async def test_child_identity_mismatch_is_rejected_and_other_child_still_read(http_root):
    hass, state, _ = http_root
    peer2 = "020000000006"
    state.members.append(peer2)
    hub = make_hub()
    hub.async_network_metadata_peers = Mock(return_value=(PEER, peer2))

    def respond(request):
        target = request.headers["Mesh-Node-Mac"].upper()
        body = device_info()
        if target != ROOT:
            body.update(self_mac=peer2, parent_mac=ROOT, layer=2)
        return web.json_response(body, headers={"Mesh-Node-Mac": target.lower()})

    state.overrides[("POST", "/device_request")] = respond
    poller = network.RootMetadataPoller(hass, hub, BINDINGS, include_children=True)
    poller._running = True
    await poller._async_poll()
    poller.async_stop()
    assert len(state.calls) == 4
    accepted = [
        call.args[0]["device_id"] for call in hub.async_accept_network_metadata.call_args_list
    ]
    assert accepted == [ROOT, peer2]
    assert (poller.child_attempted, poller.child_accepted) == (2, 1)
    assert poller.status[GROUP] == "accepted_children_partial"


async def test_child_current_qualification_is_rechecked_after_delay(http_root):
    hass, state, _ = http_root
    hub = make_hub()
    hub.async_network_metadata_peers = Mock(side_effect=[(PEER,), ()])
    poller = network.RootMetadataPoller(hass, hub, BINDINGS, include_children=True)
    poller._running = True
    await poller._async_poll()
    poller.async_stop()
    assert len(state.calls) == 2 and poller.child_attempted == 0
    assert hub.async_accept_network_metadata.call_count == 1


async def test_cancellation_during_child_http_stops_remaining_reads(http_root):
    hass, state, session = http_root
    hub = make_hub()
    hub.async_network_metadata_peers = Mock(return_value=(PEER,))
    child_entered, gate = asyncio.Event(), asyncio.Event()

    async def respond(request):
        target = request.headers["Mesh-Node-Mac"].upper()
        if target == PEER:
            child_entered.set()
            await gate.wait()
        body = device_info()
        if target != ROOT:
            body.update(self_mac=target, parent_mac=ROOT, layer=2)
        return web.json_response(body, headers={"Mesh-Node-Mac": target.lower()})

    state.overrides[("POST", "/device_request")] = respond
    poller = network.RootMetadataPoller(hass, hub, BINDINGS, include_children=True)
    poller._running = True
    poller._launch()
    task = poller._task
    try:
        await asyncio.wait_for(child_entered.wait(), 2)
        poller.async_stop()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gate.set()
    assert poller._timer is None and poller._task is None and not session.closed
    assert hub.async_accept_network_metadata.call_count == 1
    assert len(state.calls) == 3


async def test_global_child_budget_rotates_across_two_roots_without_starvation(http_root):
    hass, _, _ = http_root
    group2, root2 = "202233445566", "020000000010"
    bindings = {**BINDINGS, group2: {"host": "10.12.34.57", "root_mac": root2}}
    peers = {
        GROUP: tuple(f"0200000001{i:02X}" for i in range(10)),
        group2: tuple(f"0200000002{i:02X}" for i in range(10)),
    }
    hub = make_hub()
    hub.groups = [GROUP, group2]
    hub.async_network_metadata_peers = Mock(side_effect=lambda group: peers[group])
    attempted = []
    device_info_factory = device_info

    async def request(session, binding, *, device_info, target_mac=None):
        group = next(group for group, item in bindings.items() if item == binding)
        root = binding["root_mac"]
        if not device_info:
            return {
                "http_status": 200,
                "request_host": binding["host"],
                "headers": {
                    "mesh-node-num": "11",
                    "mesh-node-mac": ",".join((root, *peers[group])),
                },
                "body": {"status_code": 0},
            }
        body = device_info_factory()
        body.update(
            self_mac=target_mac or root,
            mesh_id=group,
            parent_mac=root2,
            layer=2 if target_mac else 1,
        )
        if target_mac:
            attempted.append((group, target_mac))
        return {
            "http_status": 200,
            "request_host": binding["host"],
            "headers": {"mesh-node-mac": target_mac or root},
            "body": body,
        }

    poller = network.RootMetadataPoller(hass, hub, bindings, include_children=True)
    poller._running = True
    with (
        patch.object(poller, "_async_request", side_effect=request),
        patch.object(network, "CHILD_REQUEST_GAP", 0),
    ):
        await poller._async_poll()
        assert len(attempted) == 16 and poller.child_attempted == 16
        await poller._async_poll()
    poller.async_stop()
    assert len(attempted) == 32
    assert set(attempted) == {(group, peer) for group, members in peers.items() for peer in members}
    assert 0 <= poller._child_cursor < 20


def test_child_flag_is_explicit_boolean():
    with pytest.raises(ValueError, match="invalid_metadata_children"):
        network.RootMetadataPoller(None, make_hub(), BINDINGS, include_children="yes")


@pytest.mark.parametrize("endpoint", ["mesh_info", "root", "child"])
async def test_duplicate_identity_headers_are_rejected_before_normalization(http_root, endpoint):
    hass, state, _ = http_root
    hub = make_hub()
    hub.async_network_metadata_peers = Mock(return_value=(PEER,))

    def mesh_response(request):
        return web.json_response(
            {"status_code": 0},
            headers=[
                ("Mesh-Node-Mac", ROOT + "," + PEER),
                ("mesh-node-mac", ROOT + "," + PEER),
                ("Mesh-Node-Num", "2"),
            ],
        )

    def device_response(request):
        target = request.headers["Mesh-Node-Mac"].upper()
        body = device_info()
        if target != ROOT:
            body.update(self_mac=target, parent_mac=ROOT, layer=2)
        duplicate = (endpoint == "root" and target == ROOT) or (
            endpoint == "child" and target == PEER
        )
        headers = [("Mesh-Node-Mac", target)]
        if duplicate:
            headers.append(("mesh-node-mac", target))
        return web.json_response(body, headers=headers)

    if endpoint == "mesh_info":
        state.overrides[("GET", "/mesh_info")] = mesh_response
    state.overrides[("POST", "/device_request")] = device_response
    poller = network.RootMetadataPoller(hass, hub, BINDINGS, include_children=True)
    poller._running = True
    await poller._async_poll()
    poller.async_stop()
    if endpoint == "child":
        assert hub.async_accept_network_metadata.call_count == 1
        assert poller.status[GROUP] == "accepted_children_partial"
    else:
        hub.async_accept_network_metadata.assert_not_called()
        assert poller.status[GROUP] == "invalid_metadata"
