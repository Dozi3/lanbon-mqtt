"""Real HA entities/registries with transport-only MQTT isolation."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import loader
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE, EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.util import dt as dt_util

from custom_components import lanbon_mqtt as component
from custom_components.lanbon_mqtt import button, light, sensor, switch
from custom_components.lanbon_mqtt.const import DOMAIN

GROUP, GROUP2 = "102233445566", "202233445566"
DIMMER, RELAY = "020000000002", "020000000004"


def inventory(*rows, total=None):
    """Independent literal twenty-byte record fixture."""
    return bytes([len(rows) if total is None else total]) + b"".join(
        bytes.fromhex(peer) + bytes([kind, output]) + bytes(12) for peer, kind, output in rows
    )


class Runtime:
    def __init__(self, hass):
        self.hass, self.platforms, self.entries = hass, {}, []
        self.done, self.connections, self.subscriptions, self.unsubscribers = {}, [], [], []
        self.publish = AsyncMock()
        self.now = 100.0
        self.changes = []
        self.subscribe_hook = None

    async def subscribe(self, hass, topic, receiver, *, qos, encoding):
        assert encoding is None and qos == 0
        if self.subscribe_hook:
            await self.subscribe_hook(topic)
        self.subscriptions.append((topic, receiver))
        unsubscribe = Mock()
        self.unsubscribers.append(unsubscribe)
        return unsubscribe

    def subscribe_done(self, hass, topic, qos, receiver):
        assert qos == 0
        self.done[topic] = receiver
        return Mock()

    def connection(self, hass, receiver):
        self.connections.append(receiver)
        return Mock()

    async def forward(self, entry, platforms):
        for name in platforms:
            module = {"sensor": sensor, "light": light, "switch": switch, "button": button}[name]
            platform = EntityPlatform(
                hass=self.hass,
                logger=logging.getLogger("lanbon_test"),
                domain=name,
                platform_name=DOMAIN,
                platform=module,
                scan_interval=timedelta(seconds=30),
                entity_namespace=None,
            )
            platform.config_entry = entry
            self.platforms[(entry.entry_id, name)] = platform
            await module.async_setup_entry(self.hass, entry, platform._async_schedule_add_entities)
        await self.hass.async_block_till_done()

    async def unload(self, entry, platforms):
        for name in platforms:
            if platform := self.platforms.get((entry.entry_id, name)):
                await platform.async_reset()
        return True

    async def setup(self, groups=None, *, controls=True):
        entry = ConfigEntry(
            version=2,
            minor_version=1,
            domain=DOMAIN,
            title="Synthetic broker",
            source="user",
            unique_id="ha_mqtt",
            options={},
            data={
                "groups": groups or [GROUP],
                "discovery_prefix": "homeassistant",
                "max_report_age": 300,
                "query_interval": 300,
                "query_on_start": True,
                "enable_controls": controls,
                "protocol_profile": "legacy_smartliving_mqtt_v1",
            },
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        self.hass.config_entries._entries[entry.entry_id] = entry
        self.entries.append(entry)
        await component.async_setup_entry(self.hass, entry)
        entry.runtime_data._now = lambda: self.now
        entry.runtime_data.policy.clock = lambda: self.now
        await self.hass.async_block_till_done()
        return entry

    async def receive(self, entry, topic, payload, *, retained=False):
        entry.runtime_data._message_received(
            ReceiveMessage(
                topic=topic,
                payload=payload,
                qos=0,
                retain=retained,
                subscribed_topic=topic,
                timestamp=dt_util.utcnow().timestamp(),
            )
        )
        await self.hass.async_block_till_done()

    async def state(
        self, entry, peer, value, *, suffix="state", channel=None, domain="light", retained=False
    ):
        address = peer if channel is None else f"{peer}-{channel:02d}"
        await self.receive(
            entry,
            f"homeassistant/{GROUP}/{domain}/{address}/{suffix}",
            str(value).encode(),
            retained=retained,
        )

    async def query(self, entry):
        hub = entry.runtime_data
        for done in self.done.values():
            done()
        if hub._query_handle:
            hub._query_handle.cancel()
            hub._query_handle = None
        await hub.async_query_groups()

    async def cache(self, entry, *rows, group=GROUP, total=None, retained=False):
        await self.receive(
            entry,
            f"lanbon/a/MAC:{group}/s2m/cmdStatusGet",
            inventory(*rows, total=total),
            retained=retained,
        )

    def entities(self, entry, name):
        return list(self.platforms[(entry.entry_id, name)].entities.values())

    async def close(self):
        for entry in self.entries:
            await component.async_unload_entry(self.hass, entry)
            await entry._async_process_on_unload(self.hass)
        await self.hass.async_block_till_done()


@pytest.fixture
async def runtime(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    loader.async_setup(hass)
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    test = Runtime(hass)

    @callback
    def changed(event):
        test.changes.append(event.data)

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, changed)
    with (
        patch.object(hass.config_entries, "async_forward_entry_setups", test.forward),
        patch.object(hass.config_entries, "async_unload_platforms", test.unload),
        patch(
            "custom_components.lanbon_mqtt.mqtt.async_wait_for_mqtt_client",
            AsyncMock(return_value=True),
        ),
        patch("custom_components.lanbon_mqtt.hub.mqtt.is_connected", return_value=True),
        patch("custom_components.lanbon_mqtt.hub.mqtt.async_subscribe", test.subscribe),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_on_subscribe_done", test.subscribe_done
        ),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_subscribe_connection_status",
            test.connection,
        ),
        patch("custom_components.lanbon_mqtt.hub.async_track_time_interval", return_value=Mock()),
        patch("custom_components.lanbon_mqtt.hub.mqtt.async_publish", test.publish),
    ):
        yield test
        await test.close()
    unsubscribe()
    await hass.async_stop(force=True)


async def test_query_waits_for_all_subscription_done_callbacks(runtime):
    entry = await runtime.setup()
    hub = entry.runtime_data
    assert len(runtime.subscriptions) == 5
    await hub.async_query_groups()
    runtime.publish.assert_not_called()
    callbacks = list(runtime.done.values())
    for done in callbacks[:-1]:
        done()
    await hub.async_query_groups()
    runtime.publish.assert_not_called()
    callbacks[-1]()
    assert hub._query_handle is not None
    await runtime.query(entry)
    runtime.publish.assert_awaited_once_with(
        runtime.hass, f"lanbon/a/MAC:{GROUP}/m2s/cmdQuery", b"\x11" + bytes(7), qos=1, retain=False
    )


async def test_cache_bootstrap_creates_real_assumed_light_and_commands_are_not_state(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    entity = runtime.entities(entry, "light")[0]
    state = runtime.hass.states.get(entity.entity_id)
    assert state.state == "on" and entity.assumed_state
    assert state.attributes["reported_state_source"] == "root_cache_query_response"
    assert state.attributes["peer_observation_age_known"] is False
    assert entity.brightness == round(73 * 255 / 100)
    runtime.publish.reset_mock()
    await entity.async_turn_off()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, f"homeassistant/{GROUP}/light/{DIMMER}/set", b"OFF", qos=0, retain=False
    )
    assert entity.is_on is True
    await runtime.state(entry, DIMMER, "OFF")
    assert entity.is_on is False and not entity.assumed_state
    assert entity.brightness == round(73 * 255 / 100)
    runtime.publish.reset_mock()
    await entity.async_turn_on(brightness=128)
    runtime.publish.assert_awaited_once_with(
        runtime.hass,
        f"homeassistant/{GROUP}/light/{DIMMER}/brightnessSet",
        b"50",
        qos=0,
        retain=False,
    )
    assert entity.is_on is False


@pytest.mark.parametrize("native_before_type", ["state", "sync"])
async def test_first_cache_cannot_override_later_direct_before_type_known(
    runtime, native_before_type
):
    entry = await runtime.setup()
    await runtime.query(entry)
    runtime.now += 1
    if native_before_type == "state":
        await runtime.state(entry, DIMMER, "OFF")
    else:
        await runtime.receive(
            entry,
            f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusSynchronous",
            b"\x00" + bytes(4) + bytes.fromhex(DIMMER) + b"\x4d",
        )
    runtime.now += 1
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert runtime.entities(entry, "light"), {
        "status": entry.runtime_data.query_status,
        "rejected": entry.runtime_data.store.rejected_messages,
        "device_detail": [device.detail for device in entry.runtime_data.store.devices.values()],
        "conflicts": entry.runtime_data.conflicts,
    }
    entity = runtime.entities(entry, "light")[0]
    assert entity.is_on is False and not entity.assumed_state


async def test_relay_requires_observed_route_and_uses_suffixless_single_gang(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    assert not runtime.entities(entry, "switch")
    await runtime.state(entry, RELAY, "OFF")
    entity = runtime.entities(entry, "switch")[0]
    runtime.publish.reset_mock()
    await entity.async_turn_on()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, f"homeassistant/{GROUP}/light/{RELAY}/set", b"ON", qos=0, retain=False
    )
    assert entity.is_on is False


@pytest.mark.parametrize("later_numbered", [False, True])
async def test_first_single_relay_cache_cannot_override_later_suffixless_report(
    runtime, later_numbered
):
    entry = await runtime.setup()
    await runtime.query(entry)
    runtime.now += 1
    await runtime.state(entry, RELAY, "OFF")
    if later_numbered:
        runtime.now += 1
        await runtime.state(entry, RELAY, "ON", domain="switch", channel=1)
    runtime.now += 1
    await runtime.cache(entry, (RELAY, 0x4A, 1))
    entity = runtime.entities(entry, "switch")[0]
    assert entity.is_on is later_numbered and not entity.assumed_state


async def test_multigang_native_feedback_is_independent(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4B, 1))
    await runtime.state(entry, RELAY, "OFF", domain="switch", channel=2)
    assert len(runtime.entities(entry, "switch")) == 1
    entity = runtime.entities(entry, "switch")[0]
    assert entity.device.channel == 2 and entity.is_on is False
    runtime.publish.reset_mock()
    await entity.async_turn_on()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, f"homeassistant/{GROUP}/switch/{RELAY}-02/set", b"ON", qos=0, retain=False
    )


async def test_native_registry_control_suppresses_new_actuator_and_command(runtime):
    registry = er.async_get(runtime.hass)
    native = registry.async_get_or_create("light", "mqtt", f"native_{DIMMER}")
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert not runtime.entities(entry, "light")
    assert registry.async_get(native.entity_id) == native
    runtime.publish.reset_mock()
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    with pytest.raises(HomeAssistantError):
        await entry.runtime_data.async_command(device, "power", "OFF")
    runtime.publish.assert_not_called()


async def test_partial_inventory_waits_for_complete_union_without_duplicate_entities(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), total=2)
    assert not runtime.entities(entry, "light")
    await runtime.cache(entry, (DIMMER, 0x4D, 73), total=2)
    assert not runtime.entities(entry, "light")
    await runtime.cache(entry, (RELAY, 0x4A, 0), total=2)
    assert len(runtime.entities(entry, "light")) == 1
    assert entry.runtime_data.query_status[GROUP].startswith("complete_root_cache")


async def test_retained_unsolicited_or_expired_inventory_never_bootstraps_state(runtime):
    entry = await runtime.setup()
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert not runtime.entities(entry, "light")
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 100), retained=True)
    assert not runtime.entities(entry, "light")
    runtime.now += 11
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert not runtime.entities(entry, "light")


async def test_same_peer_two_groups_is_conflict_not_duplicate_actuators(runtime):
    entry = await runtime.setup([GROUP, GROUP2])
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), group=GROUP)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), group=GROUP2)
    assert DIMMER in entry.runtime_data.conflicts
    assert len(runtime.entities(entry, "light")) == 1
    assert not runtime.entities(entry, "light")[0].available
    assert (
        len(
            [
                device
                for device in dr.async_get(runtime.hass).devices
                if (DOMAIN, DIMMER) in device.identifiers
            ]
        )
        == 1
    )


async def test_identical_metadata_and_nonstate_commands_do_not_churn_entities(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    payload = bytes([1, 8, 1]) + bytes(13) + bytes.fromhex(DIMMER) + (30048).to_bytes(2, "big")
    await runtime.receive(entry, "lanbon/a/L8devCompleteDetailReport", payload)
    start = len(runtime.changes)
    for _ in range(20):
        runtime.now += 1
        await runtime.receive(entry, "lanbon/a/L8devCompleteDetailReport", payload)
        await runtime.state(entry, DIMMER, "ON", suffix="set")
    assert runtime.changes[start:] == []


async def test_lease_expiry_unknown_state_keeps_manual_control_available(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    entity = runtime.entities(entry, "light")[0]
    runtime.now += 601
    entry.runtime_data._tick(dt_util.utcnow())
    assert entity.is_on is None and entity.available and entity.assumed_state
    runtime.publish.reset_mock()
    await entity.async_turn_off()
    assert runtime.publish.await_count == 1


async def test_setup_cancellation_cleans_callbacks_platforms_and_query_handles(runtime):
    count = 0
    entered = asyncio.Event()

    async def block(topic):
        nonlocal count
        count += 1
        if count == 3:
            entered.set()
            await asyncio.Future()

    runtime.subscribe_hook = block
    task = asyncio.create_task(runtime.setup())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    entry = runtime.entries[0]
    assert entry.runtime_data._stopped
    assert not entry.runtime_data._unsubscribers
    assert all(not platform.entities for platform in runtime.platforms.values())
    assert entry.runtime_data._query_handle is None


async def test_reconnect_invalidates_state_and_requeries_after_rate_limit(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    entity = runtime.entities(entry, "light")[0]
    runtime.now += 2
    runtime.connections[0](False)
    assert not entity.available and entity.is_on is None
    runtime.connections[0](True)
    assert entity.available and entity.is_on is None
    runtime.publish.reset_mock()
    await runtime.query(entry)
    runtime.publish.assert_not_called()
    assert entry.runtime_data._next_query == 160
    runtime.now = 161
    await runtime.query(entry)
    assert runtime.publish.await_count == 1
    await runtime.cache(entry, (DIMMER, 0x4D, 0))
    assert entity.is_on is False and entity.assumed_state


async def test_expired_direct_falls_back_to_matching_newer_root_cache(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    runtime.now = 101
    await runtime.state(entry, DIMMER, "ON")
    entity = runtime.entities(entry, "light")[0]
    runtime.now = 161
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    assert not entity.assumed_state
    runtime.now = 402
    entry.runtime_data._tick(dt_util.utcnow())
    assert entity.is_on is True and entity.assumed_state


async def test_reload_group_change_preserves_physical_unique_ids(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    original = runtime.entities(entry, "light")[0]
    original_id, original_uid = original.entity_id, original.unique_id
    await component.async_unload_entry(runtime.hass, entry)
    await entry._async_process_on_unload(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data=dict(entry.data, groups=[GROUP2]))
    await component.async_setup_entry(runtime.hass, entry)
    entry.runtime_data._now = lambda: runtime.now
    entry.runtime_data.policy.clock = lambda: runtime.now
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), group=GROUP2)
    reloaded = runtime.entities(entry, "light")[0]
    assert (reloaded.entity_id, reloaded.unique_id) == (original_id, original_uid)
    assert (
        len(
            [
                device
                for device in dr.async_get(runtime.hass).devices
                if (DOMAIN, DIMMER) in device.identifiers
            ]
        )
        == 1
    )


async def test_raw_diagnostics_disabled_and_capture_displays_connected(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    capture = next(
        row
        for row in runtime.entities(entry, "sensor")
        if isinstance(row, sensor.LanbonCaptureSensor)
    )
    assert runtime.hass.states.get(capture.entity_id).state == "listening"
    registry = er.async_get(runtime.hass)
    for suffix in ("output_code", "last_received"):
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{DIMMER}_device_{suffix}")
        assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_conflicting_partial_chunks_never_apply_or_create_controls(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), total=2)
    await runtime.cache(entry, (DIMMER, 0x4D, 25), total=2)
    assert not runtime.entities(entry, "light")
    assert entry.runtime_data.query_status[GROUP] == "invalid_or_conflicting_inventory"


@pytest.mark.parametrize(("requested", "expected"), [(0, b"0"), (1, b"1"), (255, b"100")])
async def test_positive_ha_brightness_never_rounds_to_native_off(runtime, requested, expected):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    entity = runtime.entities(entry, "light")[0]
    runtime.publish.reset_mock()
    await entity.async_turn_on(brightness=requested)
    runtime.publish.assert_awaited_once_with(
        runtime.hass,
        f"homeassistant/{GROUP}/light/{DIMMER}/brightnessSet",
        expected,
        qos=0,
        retain=False,
    )
    assert entity.brightness == round(73 * 255 / 100)


async def reload_entry(runtime, entry, **data):
    await component.async_unload_entry(runtime.hass, entry)
    await entry._async_process_on_unload(runtime.hass)
    if data:
        runtime.hass.config_entries.async_update_entry(entry, data=dict(entry.data, **data))
    await component.async_setup_entry(runtime.hass, entry)
    entry.runtime_data._now = lambda: runtime.now
    entry.runtime_data.policy.clock = lambda: runtime.now


async def test_relay_route_survives_actual_unload_reload_without_native_report(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4B, 2))
    await runtime.state(entry, RELAY, "ON", domain="switch", channel=2)
    original = runtime.entities(entry, "switch")[0]
    identity = (original.entity_id, original.unique_id)
    await reload_entry(runtime, entry)
    assert not runtime.entities(entry, "switch")
    assert not entry.runtime_data.routes
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4B, 2))
    recovered = runtime.entities(entry, "switch")[0]
    assert (recovered.entity_id, recovered.unique_id) == identity
    assert recovered.is_on is True and recovered.assumed_state
    runtime.publish.reset_mock()
    await recovered.async_turn_off()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, f"homeassistant/{GROUP}/switch/{RELAY}-02/set", b"OFF", qos=0, retain=False
    )
    stored = json.loads(Path(entry.runtime_data._route_store.path).read_text())["data"]
    assert stored == {
        "routes": [
            {
                "group": GROUP,
                "peer": RELAY,
                "channel": 2,
                "device_type": 0x4B,
                "native_base": f"homeassistant/{GROUP}/switch/{RELAY}-02",
            }
        ]
    }


@pytest.mark.parametrize("changed", ["type", "group", "prefix", "unknown_type"])
async def test_saved_route_requires_matching_scope_and_fresh_inventory_type(runtime, changed):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4B, 0))
    await runtime.state(entry, RELAY, "OFF", domain="switch", channel=2)
    data = {"groups": [GROUP2]} if changed == "group" else {}
    if changed == "prefix":
        data["discovery_prefix"] = "replacement"
    await reload_entry(runtime, entry, **data)
    assert not runtime.entities(entry, "switch")
    await runtime.query(entry)
    kind = {"type": 0x4A, "unknown_type": 0x55}.get(changed, 0x4B)
    await runtime.cache(entry, (RELAY, kind, 0), group=GROUP2 if changed == "group" else GROUP)
    assert not runtime.entities(entry, "switch")


async def test_route_storage_is_material_only_and_flushes_on_ha_final_write(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    store = entry.runtime_data._route_store
    with patch.object(store, "async_delay_save", wraps=store.async_delay_save) as delayed:
        for _ in range(20):
            await runtime.state(entry, RELAY, "OFF")
        assert delayed.call_count == 1
    path = Path(store.path)
    assert not path.exists()
    runtime.hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await runtime.hass.async_block_till_done()
    assert json.loads(path.read_text())["data"]["routes"][0]["peer"] == RELAY
    await component.async_remove_entry(runtime.hass, entry)
    assert not path.exists()


async def test_fresh_route_replaces_saved_single_gang_route_before_inventory(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    await runtime.state(entry, RELAY, "OFF", domain="switch", channel=1)
    await reload_entry(runtime, entry)
    await runtime.state(entry, RELAY, "OFF")
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    entity = runtime.entities(entry, "switch")[0]
    runtime.publish.reset_mock()
    await entity.async_turn_on()
    assert runtime.publish.call_args.args[1] == f"homeassistant/{GROUP}/light/{RELAY}/set"


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("native_brightness", [None, "25"])
async def test_no_argument_on_preserves_only_eligible_positive_brightness(
    runtime, expired, native_brightness
):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    if native_brightness:
        await runtime.state(entry, DIMMER, native_brightness, suffix="brightnessState")
    await runtime.state(entry, DIMMER, "OFF")
    entity = runtime.entities(entry, "light")[0]
    if expired:
        runtime.now += 601
    runtime.publish.reset_mock()
    await entity.async_turn_on()
    runtime.publish.assert_awaited_once_with(
        runtime.hass,
        f"homeassistant/{GROUP}/light/{DIMMER}/{'set' if expired else 'brightnessSet'}",
        b"ON" if expired else (native_brightness or "73").encode(),
        qos=0,
        retain=False,
    )
    assert entity.is_on is (None if expired else False)
