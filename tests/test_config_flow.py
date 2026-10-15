"""Real HA forms and lifecycle, with MQTT replaced at the transport boundary."""

import asyncio
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo
from test_discovery import BASE, ENERGY_TOPIC, GROUP, PEER, energy, native_config

from custom_components.lanbon_mqtt.config_flow import LanbonConfigFlow
from custom_components.lanbon_mqtt.const import BROKER_UNIQUE_ID, DISCOVERY_SECONDS, DOMAIN
from custom_components.lanbon_mqtt.discovery import DiscoveryCollector, DiscoveryResult, async_scan


@pytest.fixture
async def hass(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    broker_entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="mqtt",
        title="Synthetic native broker",
        data={},
        options={},
        source="user",
        unique_id=None,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    instance.config_entries._entries[broker_entry.entry_id] = broker_entry
    with (
        patch("homeassistant.components.mqtt.mqtt_config_entry_enabled", return_value=True),
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
    ):
        yield instance
    await instance.async_stop(force=True)


def flow_for(hass, source="user"):
    flow = LanbonConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.context = {"source": source}
    return flow


def settings(**changes):
    return {
        "profile_confirmed": True,
        "max_report_age": 300,
        "query_interval": 300,
        "query_on_start": True,
        "enable_controls": True,
        **changes,
    }


def entry_for(hass, *, options=None):
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Synthetic Lanbon",
        data={
            "groups": [GROUP],
            "discovery_prefix": "homeassistant",
            **{key: value for key, value in settings().items() if key != "profile_confirmed"},
        },
        options=options or {},
        source="user",
        unique_id=BROKER_UNIQUE_ID,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )
    # Register without setting up platforms or opening a network connection.
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


async def test_manual_setup_requires_explicit_profile_confirmation_and_creates_one_broker(hass):
    flow = flow_for(hass)
    with patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish:
        result = await flow.async_step_user()
        assert result["step_id"] == "user"
        result = await flow.async_step_user(
            {"discovery_mode": "manual", "discovery_prefix": "native"}
        )
        assert result["step_id"] == "manual"
        result = await flow.async_step_manual({"groups": f"{GROUP.lower()}, {PEER}"})
        assert result["step_id"] == "confirm"
        result = await flow.async_step_confirm(settings(profile_confirmed=False))
        assert result["errors"] == {"base": "profile_not_confirmed"}
        result = await flow.async_step_confirm(settings())
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["context"]["unique_id"] == BROKER_UNIQUE_ID
        assert result["data"] == {
            "groups": [GROUP, PEER],
            "discovery_prefix": "native",
            "max_report_age": 300,
            "query_interval": 300,
            "query_on_start": True,
            "enable_controls": True,
            "allow_existing_controls": False,
            "protocol_profile": "legacy_smartliving_mqtt_v1",
            "metadata_children": False,
            "root_bindings": {},
        }
        publish.assert_not_called()


async def test_invalid_manual_input_keeps_form_and_has_no_entry(hass):
    flow = flow_for(hass)
    result = await flow.async_step_manual({"groups": "000000000000"})
    assert result["errors"] == {"base": "invalid_group_id"}
    result = await flow.async_step_manual({"groups": []})
    assert result["errors"] == {"base": "no_groups"}
    result = await flow.async_step_user({"discovery_mode": "automatic", "discovery_prefix": "a/#"})
    assert result["errors"] == {"base": "invalid_prefix"}


@pytest.mark.parametrize(
    "changes",
    [{"query_interval": 59}, {"max_report_age": 29}, {"enable_controls": "yes"}, {"unexpected": 1}],
)
async def test_invalid_settings_cannot_create_trusted_configuration(hass, changes):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP})
    result = await flow.async_step_confirm(settings(**changes))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_settings"}


async def test_progress_scan_is_background_and_quiet_broker_gets_manual_fallback(hass):
    flow = flow_for(hass)
    result_future = hass.loop.create_future()

    async def scan(*args, **kwargs):
        assert kwargs["seconds"] == 35 == DISCOVERY_SECONDS
        return await result_future

    with patch("custom_components.lanbon_mqtt.config_flow.async_scan", side_effect=scan):
        result = await flow.async_step_user(
            {"discovery_mode": "automatic", "discovery_prefix": "homeassistant"}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        result_future.set_result(DiscoveryResult(()))
        await flow._scan_task
        result = await flow.async_step_scan()
        assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
        assert result["step_id"] == "manual"
        result = await flow.async_step_manual()
        assert result["errors"] == {}


async def test_scan_results_require_selection_then_confirmation(hass):
    flow = flow_for(hass)
    collector = DiscoveryCollector()
    collector.accept(ENERGY_TOPIC, energy(), retained=True)
    collector.accept(ENERGY_TOPIC, energy(PEER))
    with patch(
        "custom_components.lanbon_mqtt.config_flow.async_scan", return_value=collector.result()
    ):
        result = await flow.async_step_user(
            {"discovery_mode": "automatic", "discovery_prefix": "homeassistant"}
        )
        if result["type"] == FlowResultType.SHOW_PROGRESS:
            await flow._scan_task
            result = await flow.async_step_scan()
        assert result["step_id"] == "select"
        result = await flow.async_step_select()
        assert result["step_id"] == "select"
        result = await flow.async_step_select({"groups": [GROUP]})
        assert result["step_id"] == "confirm"
        assert not hass.config_entries.async_entries(DOMAIN)


async def test_existing_entry_blocks_another_setup_and_mqtt_flow(hass):
    entry_for(hass)
    result = await flow_for(hass).async_step_user()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    info = MqttServiceInfo(
        BASE + "/config", native_config(), 0, True, "homeassistant/+/light/+/config", 1
    )
    result = await flow_for(hass, "mqtt").async_step_mqtt(info)
    assert result["reason"] == "single_instance_allowed"


async def test_native_mqtt_config_is_only_a_hint_and_waits_for_user_before_scan(hass):
    flow = flow_for(hass, "mqtt")
    info = MqttServiceInfo(
        BASE + "/config", native_config().decode(), 0, True, "homeassistant/+/light/+/config", 1
    )
    with patch("custom_components.lanbon_mqtt.config_flow.async_scan") as scan:
        result = await flow.async_step_mqtt(info)
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "discovery_confirm"
        scan.assert_not_called()
        candidate = flow._collector.result().candidates[0]
        assert candidate.cached_seen and not candidate.current_seen
    bad = MqttServiceInfo(BASE + "/config", b"{}", 0, True, "#", 1)
    result = await flow_for(hass, "mqtt").async_step_mqtt(bad)
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_options_uses_existing_values_then_saves_without_direct_reload_or_publish(hass):
    entry = entry_for(hass, options={"query_interval": 600})
    flow = LanbonConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init()
    defaults = result["data_schema"]({"groups": GROUP, "discovery_prefix": "homeassistant"})
    assert defaults["query_interval"] == 600
    assert defaults["profile_confirmed"] is False
    with patch.object(hass.config_entries, "async_reload", new_callable=AsyncMock) as reload_entry:
        result = await flow.async_step_init(
            {
                "groups": f"{GROUP}\n{PEER}",
                "discovery_prefix": "native",
                **settings(query_interval=120),
            }
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"]["groups"] == [GROUP, PEER]
        assert result["data"]["query_interval"] == 120
        # The config entry manager stores options; the Hub listener reloads.
        reload_entry.assert_not_called()


async def test_scan_success_uses_raw_bytes_and_unsubscribes_all_filters(hass):
    unsubscribers, subscriptions = [], []

    async def subscribe(hass_arg, topic, callback, *, qos, encoding):
        assert hass_arg is hass and qos == 0 and encoding is None
        subscriptions.append(topic)
        if topic == ENERGY_TOPIC:
            callback(SimpleNamespace(topic=topic, payload=energy(), retain=False))
        unsubscribe = Mock()
        unsubscribers.append(unsubscribe)
        return unsubscribe

    with (
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
        patch("homeassistant.components.mqtt.async_subscribe", side_effect=subscribe),
        patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish,
    ):
        result = await async_scan(hass, "homeassistant", seconds=0.001)
    assert len(subscriptions) == 8
    assert result.candidates[0].group_id == GROUP
    assert all(unsubscribe.call_count == 1 for unsubscribe in unsubscribers)
    publish.assert_not_called()


async def test_cancelled_flow_unsubscribes_and_late_delivery_is_ignored(hass):
    flow = flow_for(hass)
    ready = asyncio.Event()
    unsubscribers, callbacks = [], []

    async def subscribe(hass_arg, topic, callback, **kwargs):
        callbacks.append(callback)
        unsubscribe = Mock()
        unsubscribers.append(unsubscribe)
        if len(unsubscribers) == 8:
            ready.set()
        return unsubscribe

    with (
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
        patch("homeassistant.components.mqtt.async_subscribe", side_effect=subscribe),
    ):
        result = await flow.async_step_user(
            {"discovery_mode": "automatic", "discovery_prefix": "homeassistant"}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await ready.wait()
        flow.async_remove()
        with pytest.raises(asyncio.CancelledError):
            await flow._scan_task
    assert all(unsubscribe.call_count == 1 for unsubscribe in unsubscribers)
    callbacks[0](SimpleNamespace(topic=ENERGY_TOPIC, payload=energy(), retain=False))
    # The closed scanner deliberately ignores queued callbacks.
    assert flow._scan_task.cancelled()


async def test_partial_subscription_failure_cleans_up_previous_subscriptions(hass):
    unsubscribe = Mock()
    with (
        patch("homeassistant.components.mqtt.is_connected", return_value=True),
        patch(
            "homeassistant.components.mqtt.async_subscribe",
            side_effect=[unsubscribe, HomeAssistantError("synthetic failure")],
        ),
    ):
        result = await async_scan(hass, "homeassistant", seconds=0.001)
    assert result.error == "mqtt_unavailable"
    unsubscribe.assert_called_once()


async def test_no_mqtt_client_returns_manual_error_without_subscriptions(hass):
    with (
        patch("homeassistant.components.mqtt.is_connected", return_value=False),
        patch("homeassistant.components.mqtt.async_subscribe") as subscribe,
    ):
        result = await async_scan(hass, "homeassistant", seconds=0.001)
        assert result.error == "mqtt_unavailable" and not result.candidates
        subscribe.assert_not_called()


async def test_root_metadata_bindings_are_optional_and_validated_for_selected_mesh(hass):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP})
    with patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish:
        await flow.async_step_confirm(settings(configure_network=True))
        await flow.async_step_network({"network_action": "add_edit"})
        await flow.async_step_network_select({"group": GROUP})
        await flow.async_step_network_binding({"host": "10.1.2.3", "root_mac": "001122334455"})
        result = await flow.async_step_network({"network_action": "finish"})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"]["root_bindings"] == {
            GROUP: {"host": "10.1.2.3", "root_mac": "001122334455"}
        }
        publish.assert_not_called()


@pytest.mark.parametrize(
    "binding",
    [
        {"host": "https://10.1.2.3", "root_mac": "001122334455"},
        {"host": "8.8.8.8", "root_mac": "001122334455"},
        {"host": "10.1.2.3", "root_mac": "011122334455"},
        {"host": "10.1.2.3", "root_mac": "000000000000"},
        {"host": "10.1.2.3", "root_mac": "001122334455", "port": 80},
    ],
)
async def test_invalid_root_binding_cannot_create_configuration(hass, binding):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP})
    await flow.async_step_confirm(settings(configure_network=True))
    await flow.async_step_network_select({"group": GROUP})
    result = await flow.async_step_network_binding(binding)
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_root_bindings"}


async def test_options_preserve_existing_bindings_and_can_clear_them(hass):
    binding = {GROUP: {"host": "10.1.2.3", "root_mac": "001122334455"}}
    entry = entry_for(hass, options={"root_bindings": binding})
    flow = LanbonConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init()
    defaults = result["data_schema"]({"groups": GROUP, "discovery_prefix": "homeassistant"})
    assert defaults["configure_network"] is True
    await flow.async_step_init(
        {"groups": GROUP, "discovery_prefix": "homeassistant", **settings(configure_network=True)}
    )
    await flow.async_step_network({"network_action": "remove"})
    await flow.async_step_network_remove({"group": GROUP})
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["root_bindings"] == {}


async def test_child_metadata_requires_explicit_private_root_binding(hass):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP})
    result = await flow.async_step_confirm(settings(metadata_children=True))
    assert result["step_id"] == "network"
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["errors"] == {"base": "network_required"}
    await flow.async_step_network_select({"group": GROUP})
    await flow.async_step_network_binding({"host": "10.1.2.3", "root_mac": "001122334455"})
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["metadata_children"] is True
