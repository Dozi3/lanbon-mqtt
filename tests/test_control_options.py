"""The additional-control choice is explicit, native and backward compatible."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from test_config_flow import entry_for, flow_for, settings
from test_config_flow import hass as hass

from custom_components import lanbon_mqtt as component
from custom_components.lanbon_mqtt.config_flow import LanbonConfigFlow

OPTION = "allow_existing_controls"
GROUP_A, GROUP_B = "102030405060", "A0B0C0D0E0F0"


def options_for(hass, entry):
    flow = LanbonConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    return flow


async def test_setup_additional_controls_are_off_by_default(hass):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP_A})
    form = await flow.async_step_confirm()
    assert form["data_schema"](settings())[OPTION] is False
    result = await flow.async_step_confirm(settings())
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][OPTION] is False
    assert LanbonConfigFlow.VERSION == 2


@pytest.mark.parametrize("enabled", [False, True])
async def test_setup_accepts_explicit_boolean_without_enabling_other_controls(hass, enabled):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP_A})
    with (
        patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish,
        patch("custom_components.lanbon_mqtt.network._shared_session") as session,
    ):
        result = await flow.async_step_confirm(
            settings(allow_existing_controls=enabled, enable_controls=False)
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][OPTION] is enabled
    assert result["data"]["enable_controls"] is False
    publish.assert_not_called()
    session.assert_not_called()


@pytest.mark.parametrize("invalid", ["true", "false", 1, 0, None, [], {}])
async def test_setup_rejects_non_boolean_additional_control_choice(hass, invalid):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP_A})
    result = await flow.async_step_confirm(settings(allow_existing_controls=invalid))
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_settings"}
    assert not hass.config_entries.async_entries("lanbon_mqtt")


async def test_old_options_default_false_and_round_trip_preserves_all_other_settings(hass):
    broker = hass.config_entries.async_entries("mqtt")[0]
    hass.config_entries.async_update_entry(
        broker,
        data={
            "broker": "mqtt.example.invalid",
            "port": 8883,
            "certificate": "auto",
            "tls_insecure": False,
        },
    )
    broker_before = (deepcopy(dict(broker.data)), deepcopy(dict(broker.options)))
    original_options = {
        "groups": [GROUP_A, GROUP_B],
        "discovery_prefix": "site-west/native",
        "max_report_age": 120,
        "query_interval": 900,
        "query_on_start": False,
        "enable_controls": True,
        "protocol_profile": "legacy_smartliving_mqtt_v1",
        "metadata_children": True,
        "root_bindings": {
            GROUP_A: {"host": "10.2.3.4", "root_mac": "02AABBCCDDEE"},
            GROUP_B: {"host": "172.20.4.8", "root_mac": "04AABBCCDDEE"},
        },
        "future_option": {"preserved": [1, 2]},
    }
    entry = entry_for(hass, options=deepcopy(original_options))
    original_data = deepcopy(dict(entry.data))
    for enabled in (False, True, False):
        flow = options_for(hass, entry)
        form = await flow.async_step_init()
        expected_previous = dict(entry.options).get(OPTION, False)
        shown = form["data_schema"]({"profile_confirmed": True})
        assert shown[OPTION] is expected_previous
        submitted = shown | {OPTION: enabled, "configure_network": False}
        result = await flow.async_step_init(submitted)
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == original_options | {OPTION: enabled}
        assert dict(entry.data) == original_data
        assert (dict(broker.data), dict(broker.options)) == broker_before
        assert not {"broker", "password", "certificate", "tls_insecure"} & result["data"].keys()
        hass.config_entries.async_update_entry(entry, options=result["data"])


@pytest.mark.parametrize("invalid", ["true", 1, None])
async def test_options_reject_non_boolean_without_mutating_saved_entry(hass, invalid):
    entry = entry_for(hass)
    before = (deepcopy(dict(entry.data)), deepcopy(dict(entry.options)))
    flow = options_for(hass, entry)
    result = await flow.async_step_init(
        {
            "groups": GROUP_A,
            "discovery_prefix": "homeassistant",
            **settings(allow_existing_controls=invalid),
        }
    )
    assert result["errors"] == {"base": "invalid_settings"}
    assert (dict(entry.data), dict(entry.options)) == before


@pytest.mark.parametrize(
    ("data_value", "option_value", "expected"),
    [(None, None, False), (True, None, True), (True, False, False), (False, True, True)],
)
async def test_setup_passes_merged_additional_control_choice_to_hub(
    hass, data_value, option_value, expected
):
    entry = entry_for(hass)
    data = dict(entry.data) | {"protocol_profile": "legacy_smartliving_mqtt_v1"}
    options = {}
    if data_value is not None:
        data[OPTION] = data_value
    if option_value is not None:
        options[OPTION] = option_value
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    hub = SimpleNamespace(async_load_routes=AsyncMock(), async_start=AsyncMock(), async_stop=Mock())
    with (
        patch.object(component.mqtt, "async_wait_for_mqtt_client", return_value=True),
        patch.object(component, "LanbonHub", return_value=hub) as constructor,
        patch.object(hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock),
    ):
        assert await component.async_setup_entry(hass, entry)
    assert constructor.call_args.kwargs[OPTION] is expected
    assert constructor.call_args.kwargs["enable_controls"] is True


def test_setup_and_configure_explain_separate_controls_and_prerequisite():
    component_path = Path(component.__file__).parent
    translations = json.loads((component_path / "strings.json").read_text())
    assert translations == json.loads((component_path / "translations/en.json").read_text())
    for step in (
        translations["config"]["step"]["confirm"],
        translations["options"]["step"]["init"],
    ):
        assert step["data"][OPTION] == "Add Lanbon controls alongside existing controls"
        description = step["data_description"][OPTION].lower()
        assert "separate controls" in description
        assert "entity ids" in description
        assert "off by default" in description
        assert "enable supported light and switch controls" in description
