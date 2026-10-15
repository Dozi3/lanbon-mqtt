"""Explicit read-only button batches at the real HA and MQTT-service seams."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from test_ha_diagnostics import qualified
from test_ha_runtime_v2 import DIMMER, GROUP
from test_ha_runtime_v2 import runtime as runtime


async def test_manual_button_publishes_exact_four_safe_getters_once_with_gaps(runtime):
    entry = await qualified(runtime)
    button = runtime.entities(entry, "button")[0]
    runtime.publish.reset_mock()
    times = []

    async def published(*args, **kwargs):
        times.append(runtime.hass.loop.time())

    runtime.publish.side_effect = published
    assert button.available
    await button.async_press()
    assert len(times) == 4 and all(b - a >= 0.24 for a, b in zip(times, times[1:], strict=False))
    assert [call.args[2] for call in runtime.publish.call_args_list] == [
        bytes([opcode]) + bytes.fromhex(DIMMER) + b"\x00" for opcode in range(0xA0, 0xA4)
    ]
    assert all(
        call.args[1] == f"lanbon/a/MAC:{GROUP}/m2s/cmdQuery"
        and call.kwargs == {"qos": 0, "retain": False}
        for call in runtime.publish.call_args_list
    )
    assert not button.available
    assert runtime.entities(entry, "light")[0].is_on is True
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    assert runtime.publish.await_count == 4
    runtime.now += 61
    assert button.available


async def test_parameter_batches_are_global_single_task_across_two_peers(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    second = "020000000006"
    await runtime.cache(entry, (DIMMER, 0x4D, 73), (second, 0x4D, 25))
    buttons = runtime.entities(entry, "button")
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()

    runtime.publish = AsyncMock(side_effect=blocked)
    from unittest.mock import patch

    with patch("custom_components.lanbon_mqtt.hub.mqtt.async_publish", runtime.publish):
        task = asyncio.create_task(buttons[0].async_press())
        await asyncio.wait_for(entered.wait(), 2)
        assert not buttons[1].available
        with pytest.raises(HomeAssistantError):
            await buttons[1].async_press()
        assert runtime.publish.await_count == 1
        release.set()
        await task
        assert buttons[1].available


@pytest.mark.parametrize("action", ["unload", "disconnect"])
async def test_parameter_read_cancelled_during_wait_publishes_no_further_getters(runtime, action):
    entry = await qualified(runtime)
    button = runtime.entities(entry, "button")[0]
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    runtime.publish.reset_mock()
    runtime.publish.side_effect = blocked
    task = asyncio.create_task(button.async_press())
    await asyncio.wait_for(entered.wait(), 2)
    if action == "unload":
        entry.runtime_data.async_stop()
    else:
        entry.runtime_data._connection_changed(False)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.publish.await_count == 1
    assert entry.runtime_data._parameter_task is None
    assert not entry.runtime_data._parameter_windows


async def test_owner_conflict_stops_remaining_batch_without_retry(runtime):
    entry = await qualified(runtime)
    button = runtime.entities(entry, "button")[0]
    runtime.publish.reset_mock()

    async def changed(*args, **kwargs):
        entry.runtime_data.conflicts.add(DIMMER)

    runtime.publish.side_effect = changed
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    assert runtime.publish.await_count == 1


async def test_current_generation_inventory_is_required_even_after_new_energy_membership(runtime):
    entry = await qualified(runtime)
    hub = entry.runtime_data
    button = runtime.entities(entry, "button")[0]
    hub._connection_changed(False)
    hub._connection_changed(True)
    await runtime.receive(
        entry,
        "lanbon/a/L8devElecsumReport",
        b"\x01\x01\x00" + bytes.fromhex(GROUP + DIMMER) + bytes(3),
    )
    assert not button.available
    runtime.publish.reset_mock()
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    runtime.publish.assert_not_called()


async def test_observer_only_configuration_can_offer_readonly_button_without_controls(runtime):
    entry = await runtime.setup(controls=False)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    assert not runtime.entities(entry, "light")
    assert runtime.entities(entry, "button")[0].available


async def test_publish_failure_is_not_retried_and_does_not_claim_response_window(runtime):
    entry = await qualified(runtime)
    runtime.publish.reset_mock()
    runtime.publish.side_effect = HomeAssistantError("synthetic publish failure")
    with pytest.raises(HomeAssistantError):
        await runtime.entities(entry, "button")[0].async_press()
    assert runtime.publish.await_count == 1
    assert not entry.runtime_data._parameter_windows
    assert entry.runtime_data._parameter_task is None
    assert not runtime.entities(entry, "button")[0].available
