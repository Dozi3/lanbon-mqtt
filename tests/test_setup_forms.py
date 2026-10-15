"""Universal setup uses native HA fields and preserves draft configuration."""

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
from test_config_flow import entry_for, flow_for, settings
from test_config_flow import hass as hass

from custom_components.lanbon_mqtt.config_flow import LanbonConfigFlow

GROUP_A = "102030405060"
GROUP_B = "A0B0C0D0E0F0"
ROOT_A = "02AABBCCDDEE"
ROOT_B = "04AABBCCDDEE"


async def setup_network(hass, *, groups=GROUP_A, children=False):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": groups})
    result = await flow.async_step_confirm(
        settings(configure_network=True, metadata_children=children)
    )
    assert result["step_id"] == "network"
    return flow


async def bind(flow, group, host, root):
    result = await flow.async_step_network_select({"group": group})
    assert result["step_id"] == "network_binding"
    return await flow.async_step_network_binding({"host": host, "root_mac": root})


def options_for(hass, entry):
    flow = LanbonConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    return flow


async def test_network_setup_uses_selected_mesh_then_separate_host_and_mac_fields(hass):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": "102030405060, A0B0C0D0E0F0"})
    form = await flow.async_step_confirm()
    assert not any(
        isinstance(value, selector.ObjectSelector) for value in form["data_schema"].schema.values()
    )
    with patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish:
        form = await flow.async_step_confirm(settings(configure_network=True))
        assert form["step_id"] == "network"
        form = await flow.async_step_network({"network_action": "add_edit"})
        assert form["step_id"] == "network_select"
        form = await flow.async_step_network_select({"group": "A0B0C0D0E0F0"})
        assert form["step_id"] == "network_binding"
        assert {key.schema for key in form["data_schema"].schema} == {"host", "root_mac"}
        form = await flow.async_step_network_binding(
            {"host": "172.20.9.8", "root_mac": "02:aa:bb:cc:dd:ee"}
        )
        assert form["step_id"] == "network"
        result = await flow.async_step_network({"network_action": "finish"})
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"]["root_bindings"] == {
            "A0B0C0D0E0F0": {"host": "172.20.9.8", "root_mac": "02AABBCCDDEE"}
        }
        publish.assert_not_called()


@pytest.mark.parametrize(
    ("host", "root"),
    [
        ("10.200.3.6", "02aabbccddee"),
        ("172.31.5.4", "02:aa:bb:cc:dd:ee"),
        ("192.168.77.88", "02-aa-bb-cc-dd-ee"),
    ],
)
async def test_unrelated_house_networks_and_mac_formats(hass, host, root):
    flow = await setup_network(hass)
    assert (await bind(flow, GROUP_A, host, root))["step_id"] == "network"
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["data"]["root_bindings"] == {GROUP_A: {"host": host, "root_mac": ROOT_A}}


async def test_nondefault_prefix_and_mqtt_only_setup_need_no_network_data(hass):
    flow = flow_for(hass)
    await flow.async_step_user({"discovery_mode": "manual", "discovery_prefix": "site-west/native"})
    await flow.async_step_manual({"groups": GROUP_B})
    with patch("custom_components.lanbon_mqtt.network._shared_session") as session:
        result = await flow.async_step_confirm(
            settings(enable_controls=False, query_on_start=False)
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["discovery_prefix"] == "site-west/native"
    assert result["data"]["groups"] == [GROUP_B]
    assert result["data"]["root_bindings"] == {}
    assert result["data"]["enable_controls"] is False
    assert result["data"]["query_on_start"] is False
    session.assert_not_called()


@pytest.mark.parametrize(("host", "root"), [("10.2.3.4", ROOT_B), ("172.20.4.8", ROOT_A)])
async def test_multiple_meshes_reject_duplicate_hosts_or_physical_roots(hass, host, root):
    flow = await setup_network(hass, groups=[GROUP_A, GROUP_B])
    await bind(flow, GROUP_A, "10.2.3.4", ROOT_A)
    result = await bind(flow, GROUP_B, host, root)
    assert result["errors"] == {"base": "invalid_root_bindings"}
    # Invalid retry leaves the first binding intact and offers submitted fields.
    assert result["data_schema"]({}) == {"host": host, "root_mac": root}
    await flow.async_step_network_binding({"host": "172.20.4.8", "root_mac": ROOT_B})
    result = await flow.async_step_network({"network_action": "finish"})
    assert len(result["data"]["root_bindings"]) == 2


@pytest.mark.parametrize(
    "root", ["02:aa-bb:cc:dd:ee", "02aabbccdde", " 02 aa bb cc dd ee ", "not-a-mac"]
)
async def test_invalid_mac_punctuation_has_field_error_and_can_retry(hass, root):
    flow = await setup_network(hass)
    result = await bind(flow, GROUP_A, "10.9.8.7", root)
    assert result["errors"] == {"root_mac": "invalid_root_mac"}
    result = await flow.async_step_network_binding({"host": "10.9.8.7", "root_mac": ROOT_A})
    assert result["step_id"] == "network"


async def test_mesh_dropdown_cannot_bind_an_unselected_mesh(hass):
    flow = await setup_network(hass)
    result = await flow.async_step_network_select({"group": GROUP_B})
    assert result["errors"] == {"group": "invalid_group_id"}


async def test_options_keep_offscreen_bindings_and_settings_without_contacting_broker(hass):
    original = {
        GROUP_A: {"host": "10.2.3.4", "root_mac": ROOT_A},
        GROUP_B: {"host": "172.20.4.8", "root_mac": ROOT_B},
    }
    entry = entry_for(
        hass,
        options={
            "groups": [GROUP_A, GROUP_B],
            "root_bindings": original,
            "query_interval": 900,
            "future_option": {"kept": True},
        },
    )
    flow = options_for(hass, entry)
    with (
        patch("homeassistant.components.mqtt.is_connected", return_value=False),
        patch("homeassistant.components.mqtt.async_publish", new_callable=AsyncMock) as publish,
    ):
        result = await flow.async_step_init(
            {
                "groups": [GROUP_A, GROUP_B],
                "discovery_prefix": "second-house",
                "profile_confirmed": True,
                "enable_controls": False,
                "configure_network": False,
            }
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["root_bindings"] == original
    assert result["data"]["query_interval"] == 900
    assert result["data"]["future_option"] == {"kept": True}
    assert result["data"]["enable_controls"] is False
    assert entry.options["root_bindings"] == original
    publish.assert_not_called()


async def test_options_edit_one_root_preserves_other_root_and_does_not_mutate_until_save(hass):
    original = {
        GROUP_A: {"host": "10.2.3.4", "root_mac": ROOT_A},
        GROUP_B: {"host": "172.20.4.8", "root_mac": ROOT_B},
    }
    entry = entry_for(hass, options={"groups": [GROUP_A, GROUP_B], "root_bindings": original})
    flow = options_for(hass, entry)
    await flow.async_step_init(
        {
            "groups": [GROUP_A, GROUP_B],
            "discovery_prefix": "native",
            **settings(configure_network=True),
        }
    )
    form = await flow.async_step_network_select({"group": GROUP_A})
    assert form["data_schema"]({}) == original[GROUP_A]
    await flow.async_step_network_binding({"host": "192.168.88.8", "root_mac": ROOT_A})
    assert entry.options["root_bindings"] == original
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["data"]["root_bindings"][GROUP_A]["host"] == "192.168.88.8"
    assert result["data"]["root_bindings"][GROUP_B] == original[GROUP_B]


async def test_explicit_mesh_removal_confirmation_and_last_root_child_toggle(hass):
    original = {GROUP_A: {"host": "10.2.3.4", "root_mac": ROOT_A}}
    entry = entry_for(
        hass,
        options={
            "groups": [GROUP_A, GROUP_B],
            "root_bindings": original,
            "metadata_children": True,
        },
    )
    flow = options_for(hass, entry)
    result = await flow.async_step_init(
        {
            "groups": [GROUP_B],
            "discovery_prefix": "native",
            "profile_confirmed": True,
            "configure_network": False,
        }
    )
    assert result["step_id"] == "remove_bound_groups"
    result = await flow.async_step_remove_bound_groups({"remove_bindings": False})
    assert result["errors"] == {"remove_bindings": "confirmation_required"}
    assert entry.options["root_bindings"] == original
    result = await flow.async_step_remove_bound_groups({"remove_bindings": True})
    assert result["step_id"] == "network"
    result = await flow.async_step_network({"network_action": "finish"})
    assert result["errors"] == {"base": "network_required"}
    result = await flow.async_step_network({"network_action": "finish", "metadata_children": False})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["root_bindings"] == {}
    assert result["data"]["groups"] == [GROUP_B]
    assert result["data"]["metadata_children"] is False


async def test_remove_last_root_writes_empty_dict_to_override_old_entry_data(hass):
    entry = entry_for(hass, options={"groups": [GROUP_A]})
    old = dict(entry.data) | {
        "groups": [GROUP_A],
        "root_bindings": {GROUP_A: {"host": "10.2.3.4", "root_mac": ROOT_A}},
    }
    hass.config_entries.async_update_entry(entry, data=old)
    flow = options_for(hass, entry)
    await flow.async_step_init(
        {"groups": GROUP_A, "discovery_prefix": "native", **settings(configure_network=True)}
    )
    await flow.async_step_network_remove({"group": GROUP_A})
    result = await flow.async_step_network({"network_action": "finish"})
    assert (dict(entry.data) | result["data"])["root_bindings"] == {}


async def test_native_mqtt_prerequisite_actual_public_api_absent_and_unloaded(tmp_path):
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    try:
        flow = flow_for(instance)
        result = await flow.async_step_user()
        assert result["step_id"] == "mqtt_setup"
        assert result["errors"] == {"base": "mqtt_not_configured"}
        # Native helper optimizes for a connected client before checking entries.
        # A partially unloaded native client must not satisfy setup by itself.
        instance.data["mqtt"] = SimpleNamespace(client=SimpleNamespace(connected=True))
        assert mqtt.mqtt_config_entry_enabled(instance)
        result = await flow.async_step_user()
        assert result["step_id"] == "mqtt_setup"
        assert result["errors"] == {"base": "mqtt_not_configured"}
        instance.data.pop("mqtt")
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="mqtt",
            title="Test broker",
            data={},
            options={},
            source="user",
            unique_id=None,
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        instance.config_entries._entries[entry.entry_id] = entry
        result = await flow.async_step_mqtt_setup({})
        assert result["errors"] == {"base": "mqtt_unavailable"}
        with patch("homeassistant.components.mqtt.is_connected", return_value=True):
            result = await flow.async_step_mqtt_setup({})
        assert result["step_id"] == "user"
    finally:
        await instance.async_stop(force=True)


async def test_broker_loss_at_save_retries_with_complete_network_draft(hass):
    flow = await setup_network(hass)
    await bind(flow, GROUP_A, "10.2.3.4", ROOT_A)
    with patch("homeassistant.components.mqtt.is_connected", return_value=False):
        result = await flow.async_step_network({"network_action": "finish"})
        assert result["step_id"] == "mqtt_setup"
        result = await flow.async_step_mqtt_setup({})
        assert result["errors"] == {"base": "mqtt_unavailable"}
    result = await flow.async_step_mqtt_setup({})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["root_bindings"][GROUP_A]["root_mac"] == ROOT_A


@pytest.mark.parametrize(
    "changes", [{"query_interval": 60.5}, {"max_report_age": True}, {"metadata_children": "yes"}]
)
async def test_direct_call_rejects_noninteger_timing_and_nonboolean_settings(hass, changes):
    flow = flow_for(hass)
    await flow.async_step_manual({"groups": GROUP_A})
    result = await flow.async_step_confirm(settings(**changes))
    assert result["errors"] == {"base": "invalid_settings"}


def test_setup_translation_steps_and_labels_match_and_require_no_object_editor():
    root = Path(__file__).parents[1] / "custom_components" / "lanbon_mqtt"
    english = json.loads((root / "strings.json").read_text(encoding="utf-8"))
    assert english == json.loads((root / "translations" / "en.json").read_text(encoding="utf-8"))
    for area, first in (("config", "confirm"), ("options", "init")):
        for step in (
            first,
            "network",
            "network_select",
            "network_binding",
            "network_remove",
            "remove_bound_groups",
        ):
            assert step in english[area]["step"]
            assert "root_bindings" not in english[area]["step"][step].get("data", {})
    assert "mqtt_setup" in english["config"]["step"]
    mqtt_help = english["config"]["step"]["mqtt_setup"]["description"]
    assert "/config/integrations/dashboard/add?domain=mqtt" in mqtt_help
    assert "/config/integrations/integration/mqtt" in mqtt_help
    assert "?add=mqtt" not in mqtt_help
