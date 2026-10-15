"""Independent retained-cache isolation and multi-mesh query timing regressions."""

import pytest
from test_ha_runtime_v2 import DIMMER, GROUP, GROUP2, RELAY
from test_ha_runtime_v2 import runtime as runtime


async def test_retained_other_mesh_cannot_rebind_an_existing_live_control(runtime):
    entry = await runtime.setup([GROUP, GROUP2])
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), group=GROUP)
    await runtime.state(entry, DIMMER, "ON")
    entity = runtime.entities(entry, "light")[0]
    assert entity.is_on is True
    original_owner = entry.runtime_data.bindings.peer_owners[DIMMER]
    await runtime.receive(
        entry, f"homeassistant/{GROUP2}/light/{DIMMER}/state", b"OFF", retained=True
    )
    assert entity.device.group_id == original_owner
    assert entity.is_on is True
    await entity.async_turn_off()


async def test_cached_unqualified_devices_cannot_fill_live_entity_capacity(runtime):
    entry = await runtime.setup()
    hub = entry.runtime_data
    # Injecting a small cap reproduces the same default256 capacity boundary.
    hub.store.max_devices = 2
    for peer in ("020000000010", "020000000012"):
        await runtime.state(entry, peer, "ON", retained=True)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    assert (DIMMER, None) in hub.store.devices
    assert runtime.entities(entry, "light")


async def test_second_group_query_window_starts_at_its_own_publication(runtime):
    entry = await runtime.setup([GROUP, GROUP2])
    hub = entry.runtime_data
    for callback in runtime.done.values():
        callback()
    if hub._query_handle:
        hub._query_handle.cancel()
        hub._query_handle = None

    async def delayed_publish(hass, topic, payload, **kwargs):
        if f"MAC:{GROUP}/" in topic:
            # A first QoS1 publish may await ACK while a later group waits.
            runtime.now += 11
        elif f"MAC:{GROUP2}/" in topic:
            await runtime.cache(entry, (DIMMER, 0x4D, 73), group=GROUP2)

    runtime.publish.side_effect = delayed_publish
    await hub.async_query_groups()
    assert runtime.entities(entry, "light")


@pytest.mark.parametrize(("device_type", "channel"), [(0x4A, 2), (0x4B, 3), (0x4C, 4)])
async def test_native_route_outside_inventoried_gang_count_never_creates_control(
    runtime, device_type, channel
):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, device_type, 0))
    await runtime.state(entry, RELAY, "ON", domain="switch", channel=channel)
    assert not runtime.entities(entry, "switch")
