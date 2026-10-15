"""Actual HA platform controls coexist only after explicit opt-in."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from test_ha_runtime_v2 import DIMMER, GROUP, RELAY
from test_ha_runtime_v2 import runtime as runtime

from custom_components import lanbon_mqtt as component
from custom_components.lanbon_mqtt.policy import SourceKind

DIMMER2 = "020000000006"


async def reload_entry(test, entry, *, allow, controls=True):
    await component.async_unload_entry(test.hass, entry)
    await entry._async_process_on_unload(test.hass)
    test.hass.config_entries.async_update_entry(
        entry,
        options=dict(entry.options)
        | {"allow_existing_controls": allow, "enable_controls": controls},
    )
    await component.async_setup_entry(test.hass, entry)
    entry.runtime_data._now = lambda: test.now
    entry.runtime_data.policy.clock = lambda: test.now
    await test.hass.async_block_till_done()


async def setup(test, *, allow=False, controls=True):
    entry = await test.setup(controls=controls)
    await reload_entry(test, entry, allow=allow, controls=controls)
    return entry


def native(test, peer, *, domain="light", category=None, hidden=False):
    return er.async_get(test.hass).async_get_or_create(
        domain,
        "mqtt",
        f"native_{peer}_{domain}",
        entity_category=category,
        hidden_by=er.RegistryEntryHider.USER if hidden else None,
    )


def debug_routes(test, entities):
    test.hass.data["mqtt"] = SimpleNamespace(
        debug_info_entities={
            entity.entity_id: {
                "subscriptions": {topic: {"count": 1, "messages": [b"ON"]} for topic in topics},
                "discovery_data": {"discovery_payload": config},
            }
            for entity, topics, config in entities
        }
    )


def status(test, entry, peer):
    return next(
        entity
        for entity in test.entities(entry, "sensor")
        if entity.unique_id == f"{peer}_device_control_status"
    )


@pytest.mark.parametrize(
    ("controls", "allow", "expected"),
    [(False, False, 0), (False, True, 0), (True, False, 0), (True, True, 2)],
)
async def test_two_dimmers_global_gate_and_explicit_coexistence(runtime, controls, allow, expected):
    registry = er.async_get(runtime.hass)
    originals = [native(runtime, peer) for peer in (DIMMER, DIMMER2)]
    entry = await setup(runtime, allow=allow, controls=controls)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73), (DIMMER2, 0x4D, 40))
    assert len(runtime.entities(entry, "light")) == expected
    assert [registry.async_get(entity.entity_id) for entity in originals] == originals
    detail = status(runtime, entry, DIMMER)
    assert detail.entity_registry_enabled_default
    assert detail.native_value == (
        "disabled" if not controls else "ready" if allow else "existing_controls_preserved"
    )
    assert originals[0].entity_id in detail.extra_state_attributes["existing_control_entities"]
    if expected:
        actuator = runtime.entities(entry, "light")[0]
        assert actuator.is_on and actuator.assumed_state
        runtime.publish.reset_mock()
        await actuator.async_turn_off()
        runtime.publish.assert_awaited_once_with(
            runtime.hass,
            f"homeassistant/{GROUP}/light/{actuator.device.device_id}/set",
            b"OFF",
            qos=0,
            retain=False,
        )
        assert actuator.is_on  # Publishing is intent, not reported state.


@pytest.mark.parametrize("category", [EntityCategory.CONFIG, EntityCategory.DIAGNOSTIC])
async def test_network_administration_entity_is_not_output_ownership(runtime, category):
    registry = er.async_get(runtime.hass)
    administrative = registry.async_get_or_create(
        "switch", "netgear", f"allow_access_{DIMMER}", entity_category=category
    )
    entry = await setup(runtime)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert len(runtime.entities(entry, "light")) == 1
    assert not entry.runtime_data.suppressed(DIMMER)
    assert status(runtime, entry, DIMMER).extra_state_attributes["existing_control_entities"] == ()
    assert registry.async_get(administrative.entity_id) == administrative


async def test_hidden_functional_native_entity_still_counts_as_owner(runtime):
    entity = native(runtime, DIMMER, hidden=True)
    entry = await setup(runtime)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 100))
    assert not runtime.entities(entry, "light")
    assert entity.entity_id in entry.runtime_data.existing_control_entities(DIMMER)


@pytest.mark.parametrize(
    ("kind", "domain", "channels"), [(0x4A, "light", [1]), (0x4B, "switch", [1, 2])]
)
async def test_native_yaml_subscriptions_supply_relay_routes_not_reported_state(
    runtime, kind, domain, channels
):
    entity = native(runtime, RELAY, domain=domain, hidden=True)
    topics = [f"homeassistant/{GROUP}/switch/{RELAY}-{channel:02}/state" for channel in channels]
    debug_routes(runtime, [(entity, topics, {})])
    entry = await setup(runtime, allow=True)
    assert not entry.runtime_data.routes  # No inventory/type qualification yet.
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, kind, 0))
    entities = runtime.entities(entry, "switch")
    assert len(entities) == len(channels)
    assert all(entity.is_on is False and entity.assumed_state for entity in entities)
    assert all(
        value.source is SourceKind.ROOT_CACHE for value in entry.runtime_data.policy.fields.values()
    )
    assert not entry.runtime_data._route_records  # Metadata-only routes are not persisted.
    runtime.publish.reset_mock()
    await entities[-1].async_turn_on()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, topics[-1].removesuffix("state") + "set", b"ON", qos=0, retain=False
    )
    assert entities[-1].is_on is False
    assert status(runtime, entry, RELAY).native_value == "ready"


@pytest.mark.parametrize(
    ("suffix", "config", "count"),
    [
        (f"homeassistant/{GROUP}/switch/{RELAY}-03/state", {}, 1),
        (f"wrong/{GROUP}/switch/{RELAY}-01/state", {}, 1),
        (f"homeassistant/202233445566/switch/{RELAY}-01/state", {}, 1),
        (f"homeassistant/{GROUP}/switch/020000000008-01/state", {}, 1),
        (f"homeassistant/{GROUP}/switch/{RELAY}-01/+", {}, 1),
        (f"homeassistant/{GROUP}/switch/{RELAY}-01/state", {"cmd_t": "other/set"}, 1),
        (f"homeassistant/{GROUP}/switch/{RELAY}-01/state", {"command_topic": "other/set"}, 1),
        (f"homeassistant/{GROUP}/switch/{RELAY}-01/state", {}, 0),
    ],
)
async def test_route_metadata_fails_closed_for_other_scope_aliases_and_channels(
    runtime, suffix, config, count
):
    entity = native(runtime, RELAY, domain="switch")
    debug_routes(runtime, [(entity, [suffix], config)])
    runtime.hass.data["mqtt"].debug_info_entities[entity.entity_id]["subscriptions"][suffix][
        "count"
    ] = count
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4B, 0))
    assert not runtime.entities(entry, "switch")
    assert not entry.runtime_data.routes
    assert status(runtime, entry, RELAY).native_value == "waiting_for_route"


async def test_metadata_ambiguity_revokes_only_metadata_route_and_real_feedback_upgrades_it(
    runtime,
):
    entity = native(runtime, RELAY)
    first = f"homeassistant/{GROUP}/switch/{RELAY}-01/state"
    second = f"homeassistant/{GROUP}/light/{RELAY}/state"
    debug_routes(runtime, [(entity, [first], {})])
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    control = runtime.entities(entry, "switch")[0]
    assert control.available
    debug_routes(runtime, [(entity, [first, second], {})])
    assert entry.runtime_data._learn_native_routes()
    assert entry.runtime_data.route_for(RELAY, 1) is None
    assert control.available is False
    assert "Conflicting" in status(runtime, entry, RELAY).extra_state_attributes["explanation"]
    await runtime.state(entry, RELAY, "OFF", domain="switch", channel=1)
    assert control.available
    assert entry.runtime_data._route_records
    entry.runtime_data._learn_native_routes()
    assert entry.runtime_data.route_for(RELAY, 1) == first.removesuffix("/state")


async def test_opted_in_route_requires_current_owner_generation_and_membership_every_use(runtime):
    entity = native(runtime, RELAY)
    topic = f"homeassistant/{GROUP}/switch/{RELAY}-01/state"
    debug_routes(runtime, [(entity, [topic], {})])
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    control = runtime.entities(entry, "switch")[0]
    runtime.now += 301
    assert entry.runtime_data.route_for(RELAY, 1) is None
    assert control.available is False
    runtime.publish.reset_mock()
    with pytest.raises(HomeAssistantError):
        await control.async_turn_on()
    runtime.publish.assert_not_called()
    runtime.now += 1
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    assert control.available
    entry.runtime_data._connection_changed(False)
    entry.runtime_data._connection_changed(True)
    assert entry.runtime_data.route_for(RELAY, 1) is None
    entry.runtime_data.conflicts.add(RELAY)
    assert control.available is False


async def test_option_disable_reenable_preserves_both_platform_registry_identities(runtime):
    original = native(runtime, DIMMER)
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    own = runtime.entities(entry, "light")[0]
    identity, entity_id = own.unique_id, own.entity_id
    await reload_entry(runtime, entry, allow=False)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    assert not runtime.entities(entry, "light")
    registry = er.async_get(runtime.hass)
    assert registry.async_get(original.entity_id) == original
    assert registry.async_get(entity_id).unique_id == identity
    await reload_entry(runtime, entry, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    assert runtime.entities(entry, "light")[0].entity_id == entity_id
    assert registry.async_get(original.entity_id) == original


async def test_own_diagnostic_registry_creation_does_not_refresh_every_listener(runtime):
    entry = await setup(runtime, allow=True)
    hub = entry.runtime_data
    listener = Mock()
    hub.async_add_listener(listener, "diagnostic")
    hub._registry_changed(SimpleNamespace(data={"entity_id": "sensor.some_new_diagnostic"}))
    assert hub._registry_refresh_handle is None
    assert not listener.called
    native(runtime, DIMMER)
    await runtime.hass.async_block_till_done()
    listener.reset_mock()
    event = SimpleNamespace(data={"entity_id": "light.external_functional"})
    hub._registry_changed(event)
    handle = hub._registry_refresh_handle
    hub._registry_changed(event)
    assert hub._registry_refresh_handle is handle
    await runtime.hass.async_block_till_done()
    assert listener.call_count == 1


async def test_current_native_metadata_retargets_without_changing_state_or_stronger_feedback(
    runtime,
):
    entity = native(runtime, RELAY)
    first = f"homeassistant/{GROUP}/switch/{RELAY}-01/state"
    second = f"homeassistant/{GROUP}/light/{RELAY}/state"
    debug_routes(runtime, [(entity, [first], {})])
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    control = runtime.entities(entry, "switch")[0]
    debug_routes(runtime, [(entity, [second], {})])
    # The actual command must revalidate even before a registry/tick callback.
    runtime.publish.reset_mock()
    await control.async_turn_on()
    runtime.publish.assert_awaited_once_with(
        runtime.hass, second.removesuffix("state") + "set", b"ON", qos=0, retain=False
    )
    assert control.is_on is False and control.assumed_state
    await runtime.state(entry, RELAY, "OFF", domain="switch", channel=1)
    entry.runtime_data._learn_native_routes()
    assert entry.runtime_data.route_for(RELAY, 1) == first.removesuffix("/state")


@pytest.mark.parametrize(
    "withdrawal", ["zero_count", "disabled", "removed", "empty", "wrong_command_alias"]
)
async def test_actual_command_rejects_withdrawn_or_contradicted_metadata_without_waiting(
    runtime, withdrawal
):
    entity = native(runtime, RELAY)
    topic = f"homeassistant/{GROUP}/switch/{RELAY}-01/state"
    debug_routes(runtime, [(entity, [topic], {})])
    entry = await setup(runtime, allow=True)
    await runtime.query(entry)
    await runtime.cache(entry, (RELAY, 0x4A, 0))
    control = runtime.entities(entry, "switch")[0]
    debug = runtime.hass.data["mqtt"].debug_info_entities[entity.entity_id]
    if withdrawal == "zero_count":
        debug["subscriptions"][topic]["count"] = 0
    elif withdrawal == "disabled":
        er.async_get(runtime.hass).async_update_entity(
            entity.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )
    elif withdrawal == "removed":
        er.async_get(runtime.hass).async_remove(entity.entity_id)
    elif withdrawal == "empty":
        debug["subscriptions"].clear()
    else:
        debug["discovery_data"]["discovery_payload"]["cmd_t"] = "contradicted/set"
    runtime.publish.reset_mock()
    with pytest.raises(HomeAssistantError):
        await control.async_turn_on()
    runtime.publish.assert_not_called()
    assert entry.runtime_data.route_for(RELAY, 1) is None
    assert control.available is False
