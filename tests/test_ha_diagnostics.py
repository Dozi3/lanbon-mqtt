"""Diagnostic coverage, actual HA publication and bounded provenance contracts."""

import json
from datetime import timedelta

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_ha_runtime_v2 import DIMMER, GROUP, GROUP2
from test_ha_runtime_v2 import runtime as runtime

from custom_components.lanbon_mqtt.diagnostics import async_get_config_entry_diagnostics
from custom_components.lanbon_mqtt.observation import ObservationStore
from custom_components.lanbon_mqtt.sensor import LanbonDetailSensor, LanbonGroupDetailsSensor


def detail_entity(runtime, entry, key):
    for entity in runtime.entities(entry, "sensor"):
        if isinstance(entity, LanbonDetailSensor) and entity.key == key:
            return entity
    # Default-disabled diagnostics are present in the actual entity registry but
    # are intentionally not active EntityPlatform entities. Exercise their real
    # class getters, without falsely enabling them through this transport fixture.
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    entity = LanbonDetailSensor(entry.runtime_data, device, key)
    entity.hass = runtime.hass
    entity.entity_id = er.async_get(runtime.hass).async_get_entity_id(
        "sensor", "lanbon_mqtt", entity.unique_id
    )
    assert entity.entity_id is not None
    return entity


async def qualified(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.cache(entry, (DIMMER, 0x4D, 73))
    return entry


async def firmware(runtime, entry, number):
    await runtime.receive(
        entry,
        "lanbon/a/L8devCompleteDetailReport",
        bytes([1, 8, 1]) + bytes(13) + bytes.fromhex(DIMMER) + number.to_bytes(2, "big"),
    )


async def flush(runtime, entry):
    runtime.now += 61
    entry.runtime_data._tick(dt_util.utcnow())
    if entry.runtime_data._query_handle:
        entry.runtime_data._query_handle.cancel()
        entry.runtime_data._query_handle = None
    await runtime.hass.async_block_till_done()


async def test_firmware_change_publishes_without_output_and_updates_owned_device(runtime):
    entry = await qualified(runtime)
    await firmware(runtime, entry, 30048)
    entity = detail_entity(runtime, entry, "firmware_version")
    identity = entity.unique_id, entity.entity_id
    assert runtime.hass.states.get(entity.entity_id).state == "3.0.48"
    await firmware(runtime, entry, 30049)
    assert runtime.hass.states.get(entity.entity_id).state == "3.0.48"
    await flush(runtime, entry)
    assert runtime.hass.states.get(entity.entity_id).state == "3.0.49"
    device = dr.async_get(runtime.hass).async_get_device_by_identifier(
        ("lanbon_mqtt", DIMMER), entry.entry_id
    )
    assert device.sw_version == "3.0.49" and device.model == "Dimmer"
    assert (entity.unique_id, entity.entity_id) == identity
    assert not device.connections
    assert runtime.entities(entry, "light")[0].is_on is True


async def test_inventory_every_row_leaf_retained_and_unqualified_units_absent(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    await runtime.receive(
        entry,
        f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        b"\x01" + bytes.fromhex(DIMMER) + bytes.fromhex("4d643f8080232f00010201640000"),
    )
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    row = device.details["inventory"].value
    assert row["flags_raw_hex"] == "3f80" and row["auxiliary_hex"] == "01640000"
    assert row["source_profile"]["temperature_value"] == 36.47
    assert row["source_profile"]["feature_flags"]["locked"] is None
    entity = detail_entity(runtime, entry, "temperature_decoder_candidate")
    assert entity.native_value == 36.47 and entity.native_unit_of_measurement is None
    assert entity.device_class is None and entity.state_class is None
    registry = er.async_get(runtime.hass)
    assert registry.async_get(entity.entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert not any(key.startswith("root_") for key in device.diagnostics)
    attrs = detail_entity(runtime, entry, "device_details").extra_state_attributes
    assert attrs["reports"]["inventory"]["source"] == "root_cache_query_response"
    assert not attrs["peer_observation_age_known"]


@pytest.mark.parametrize(
    "mode,payload,key,expected",
    [
        ("delay", bytes.fromhex("025801"), "timer_delay_duration", 600),
        ("greenMode", bytes.fromhex("0258"), "timer_greenMode_duration", 600),
        (
            "nightMode",
            bytes.fromhex("010203040506"),
            "timer_nightMode_schedule_raw",
            "010203040506",
        ),
    ],
)
async def test_timer_reports_are_readonly_source_facts_not_running_countdowns(
    runtime, mode, payload, key, expected
):
    entry = await qualified(runtime)
    await runtime.receive(
        entry, f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/{mode}", payload + bytes.fromhex(DIMMER)
    )
    entity = detail_entity(runtime, entry, key)
    assert entity.native_value == expected
    assert entity.native_unit_of_measurement == (None if mode == "nightMode" else "s")
    assert entity.extra_state_attributes["countdown_running_asserted"] is False
    assert entity.extra_state_attributes["live_firmware_compatibility_verified"] is False
    assert runtime.entities(entry, "light")[0].is_on is True


async def test_eight_timer_slots_and_extension_are_bounded_and_fully_inspectable(runtime):
    entry = await qualified(runtime)
    payload = bytes.fromhex("c1691e") + bytes(21) + bytes.fromhex(DIMMER) + bytes(range(8))
    await runtime.receive(entry, f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/normal", payload)
    entity = detail_entity(runtime, entry, "timer_slot_1")
    assert entity.native_value == "09:30"
    assert entity.extra_state_attributes["reported_fields"]["weekdays"] == ["Monday", "Sunday"]
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    assert len(device.details["timer_normal"].value["fields"]["slots"]) == 8
    assert device.diagnostics["timer_extension_raw"].value == "0001020304050607"
    assert len([key for key in device.diagnostics if key.startswith("timer_slot_")]) == 8


async def test_energy_raw_diagnostics_and_group_headers_never_claim_energy_units(runtime):
    entry = await qualified(runtime)
    await runtime.receive(
        entry,
        "lanbon/a/L8devElecsumReport",
        b"\x01\x01\x00" + bytes.fromhex(GROUP) + bytes.fromhex(DIMMER) + bytes(3),
    )
    entity = detail_entity(runtime, entry, "energy_measurement_raw")
    assert entity.native_value == "000000"
    assert entity.native_unit_of_measurement is None and entity.state_class is None
    group_entity = next(
        e for e in runtime.entities(entry, "sensor") if isinstance(e, LanbonGroupDetailsSensor)
    )
    assert group_entity.extra_state_attributes["report"]["energy"]["timezone_raw_hex"] == "0100"
    assert "inventory" in group_entity.extra_state_attributes["report"]


async def test_identical_detail_updates_do_not_churn_ordinary_sensors_even_after_flush(runtime):
    entry = await qualified(runtime)
    await firmware(runtime, entry, 30048)
    await flush(runtime, entry)
    before = len(runtime.changes)
    for _ in range(20):
        await firmware(runtime, entry, 30048)
    await flush(runtime, entry)
    changes = runtime.changes[before:]
    # Capture counters are deliberately coalesced; ordinary metadata/details stay identical.
    assert all(change["entity_id"].endswith("capture_status") for change in changes)


async def test_retained_details_are_separate_and_cannot_allocate_or_replace_live_values(runtime):
    entry = await qualified(runtime)
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    await runtime.cache(entry, (DIMMER, 0x4D, 0), retained=True)
    assert device.details["inventory"].value["status_raw"] == 73
    assert device.cached_details["inventory"].value["status_raw"] == 0
    assert device.cached_details["inventory"].retained
    original = len(entry.runtime_data.store.devices)
    await runtime.state(entry, "020000000099", "ON", retained=True)
    assert len(entry.runtime_data.store.devices) == original
    assert runtime.entities(entry, "light")[0].is_on is True
    await runtime.cache(entry, (DIMMER, 0x4D, 0), retained=None)
    assert device.cached_details["inventory"].source == "unknown_retain_cache"


async def test_metadata_stays_historical_while_dynamic_diagnostics_expire_and_reconnect_invalidates(
    runtime,
):
    entry = await qualified(runtime)
    await firmware(runtime, entry, 30048)
    entity = detail_entity(runtime, entry, "inventory_flags_raw")
    observation = entity.device.diagnostics["inventory_flags_raw"]
    observation.received -= timedelta(seconds=601)
    assert not entity.available
    entry.runtime_data._connection_changed(False)
    entry.runtime_data._connection_changed(True)
    assert not entity.available
    metadata = detail_entity(runtime, entry, "firmware_version")
    assert metadata.available and metadata.native_value == "3.0.48"
    assert metadata.extra_state_attributes["capture_current"] is False


async def test_network_root_ingress_requires_current_owner_and_never_qualifies_state(runtime):
    entry = await qualified(runtime)
    hub = entry.runtime_data
    metadata = {
        "group_id": GROUP,
        "device_id": DIMMER,
        "fields": {
            "rssi": -51,
            "rssi_raw": -51,
            "version": "1.1.0",
            "characteristics": [{"cid": 0, "value": 0}],
        },
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
        "observed_at": dt_util.utcnow().isoformat(),
    }
    assert hub.async_accept_network_metadata(metadata)
    await runtime.hass.async_block_till_done()
    assert detail_entity(runtime, entry, "network_rssi").native_unit_of_measurement == "dBm"
    assert detail_entity(runtime, entry, "network_version").native_value == "1.1.0"
    assert runtime.entities(entry, "light")[0].is_on is True
    assert not hub.async_accept_network_metadata(metadata | {"group_id": GROUP2})
    assert not hub.async_accept_network_metadata(metadata | {"device_id": "020000000099"})
    assert hub.async_accept_network_metadata(
        metadata | {"fields": {"rssi": None, "rssi_raw": -120, "rssi_fallback": True}}
    )
    assert not detail_entity(runtime, entry, "network_rssi").available
    hub._connection_changed(False)
    hub._connection_changed(True)
    assert not hub.async_accept_network_metadata(metadata)


async def test_new_diagnostics_export_redacts_ids_and_native_raw_records(runtime):
    entry = await qualified(runtime)
    exported = json.dumps(await async_get_config_entry_diagnostics(runtime.hass, entry))
    assert DIMMER.lower() not in exported.lower() and GROUP.lower() not in exported.lower()
    assert "root_cache_query_response" in exported


async def test_network_scalar_wrappers_redact_private_addresses_and_names(runtime):
    entry = await qualified(runtime)
    assert entry.runtime_data.async_accept_network_metadata(
        {
            "group_id": GROUP,
            "device_id": DIMMER,
            "fields": {"ip_address": "10.12.34.56", "name": "PrivateHouseholdRootLabel"},
            "source": "read_only_http_metadata",
            "profile": "mlink_get_device_info_v1",
        }
    )
    exported = json.dumps(await async_get_config_entry_diagnostics(runtime.hass, entry))
    assert "10.12.34.56" not in exported and "PrivateHouseholdRootLabel" not in exported
    assert "read_only_http_metadata" in exported


def test_diagnostic_maps_are_bounded_and_cached_data_never_allocates():
    store = ObservationStore(GROUP, max_devices=2)
    store.set_connected(True)
    now = dt_util.utcnow()
    args = {"received": now, "source": "synthetic_report", "confidence": "structural"}
    assert not store.record_details(DIMMER, GROUP, "inventory", {}, {}, retained=True, **args)
    for family in range(20):
        store.record_details(
            DIMMER, GROUP, f"family_{family}", {}, {f"key_{i}": i for i in range(80)}, **args
        )
    device = store.devices[(DIMMER, None)]
    assert len(device.details) == 16 and len(device.diagnostics) == 64
    with pytest.raises(ValueError):
        store.record_details(DIMMER, GROUP, "oversize", {"raw": "x" * 513}, {}, **args)
    with pytest.raises(ValueError):
        store.record_details(DIMMER, GROUP, "nonfinite", {"raw": float("nan")}, {}, **args)


@pytest.mark.parametrize(
    "mode,parameter_hex",
    [
        ("normal", "1e49171e0b29" + "00" * 18),
        ("delay", "000000"),
        ("greenMode", "0000"),
        ("nightMode", "0000150b0611"),
    ],
)
async def test_actual_getter_parameter_shapes_survive_ha_adapter_without_execution_claim(
    runtime, mode, parameter_hex
):
    """Synthetic parameter values preserve qualified getter layouts and flags."""
    entry = await qualified(runtime)
    await runtime.receive(
        entry, f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/{mode}", bytes.fromhex(parameter_hex + DIMMER)
    )
    stored = entry.runtime_data.store.devices[(DIMMER, None)].details[f"timer_{mode}"].value
    assert stored["fields"]
    if mode == "normal":
        entity = detail_entity(runtime, entry, "timer_slot_1")
        assert entity.native_value == "09:23"
        assert entity.extra_state_attributes["reported_fields"]["enabled"] is False
        assert entity.extra_state_attributes["reported_fields"]["action_raw"] == 2
        assert entity.extra_state_attributes["countdown_running_asserted"] is False
    elif mode == "nightMode":
        assert detail_entity(runtime, entry, "timer_night_start").native_value == "21:11"
        assert detail_entity(runtime, entry, "timer_night_end").native_value == "06:17"
        assert detail_entity(runtime, entry, "timer_night_all_day_candidate").native_value == "off"
        assert (
            detail_entity(runtime, entry, "timer_night_scheduled_candidate").native_value == "off"
        )


async def test_independent_root_timer_bits_never_create_aggregate_or_child_assertions(runtime):
    entry = await qualified(runtime)
    hub = entry.runtime_data
    envelope = {
        "group_id": GROUP,
        "device_id": DIMMER,
        "fields": {},
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
    }
    assert hub.async_accept_network_metadata(envelope)
    runtime.now += 61
    await runtime.query(entry)
    await runtime.receive(
        entry,
        f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        b"\x01" + bytes.fromhex(DIMMER) + bytes.fromhex("4d64020000000000000000000000"),
    )
    values = hub.store.devices[(DIMMER, None)].diagnostics
    assert values["root_normal_timer_running_candidate"].value is False
    assert values["root_delay_active_candidate"].value is True
    assert "root_timer_enabled_candidate" not in values


@pytest.mark.parametrize("count", [33, 256])
async def test_full_supported_mesh_membership_is_retained_without_losing_rssi(runtime, count):
    entry = await qualified(runtime)
    members = [f"02000000{i:04X}" for i in range(count)]
    metadata = {
        "group_id": GROUP,
        "device_id": DIMMER,
        "fields": {"rssi": -51, "mesh_members": members, "mesh_node_count": count},
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
    }
    assert entry.runtime_data.async_accept_network_metadata(metadata)
    device = entry.runtime_data.store.devices[(DIMMER, None)]
    assert device.diagnostics["network_rssi"].value == -51
    assert len(device.details["network"].value["fields"]["mesh_members"]) == count


async def test_child_metadata_never_replaces_root_identity_or_invents_child_address(runtime):
    entry = await runtime.setup()
    await runtime.query(entry)
    child = "020000000006"
    await runtime.cache(entry, (DIMMER, 0x4D, 73), (child, 0x4D, 25))
    hub = entry.runtime_data
    base = {
        "group_id": GROUP,
        "source": "read_only_http_metadata",
        "profile": "mlink_get_device_info_v1",
    }
    assert hub.async_accept_network_metadata(
        base | {"device_id": DIMMER, "fields": {"ip_address": "10.1.2.3"}}
    )
    assert hub.async_accept_network_metadata(
        base
        | {
            "device_id": child,
            "root_mac": DIMMER,
            "role": "child",
            "fields": {"gateway_address": "10.1.2.3", "rssi": -60},
        }
    )
    assert hub._network_roots[GROUP] == DIMMER
    device = hub.store.devices[(child, None)]
    assert "network_ip_address" not in device.diagnostics
    assert device.diagnostics["network_gateway_address"].value == "10.1.2.3"
    assert set(hub.async_network_metadata_peers(GROUP)) == {DIMMER, child}
    hub._connection_changed(False)
    hub._connection_changed(True)
    assert hub.async_network_metadata_peers(GROUP) == ()
    assert not hub.async_accept_network_metadata(
        base | {"device_id": child, "root_mac": DIMMER, "role": "child", "fields": {}}
    )


async def test_all_report_families_fit_one_physical_device_budget(runtime):
    entry = await qualified(runtime)
    hub = entry.runtime_data
    fields = {
        "self_mac": DIMMER,
        "mesh_id": GROUP,
        "parent_mac": "020000000008",
        "ip_address": "10.1.2.3",
        "rssi": -51,
        "rssi_raw": -51,
        "rssi_fallback": False,
        "layer": 1,
        "version": "1.1.0",
        "idf_version": "v3.3.5",
        "mdf_version": "",
        "mlink_version": 2,
        "mlink_trigger": 0,
        "tsf_time_raw": "1234567890123",
        "tid": "1",
        "project_name": "test",
        "name": "node",
        "status_code": 0,
        "status_msg": "MDF_OK",
        "mesh_node_count": 256,
        "mesh_members": [f"02000000{i:04X}" for i in range(256)],
        "characteristics": [{"cid": 0, "value": 0}],
    }
    assert hub.async_accept_network_metadata(
        {
            "group_id": GROUP,
            "device_id": DIMMER,
            "fields": fields,
            "source": "read_only_http_metadata",
            "profile": "mlink_get_device_info_v1",
        }
    )
    runtime.now += 61
    await runtime.query(entry)
    await runtime.receive(
        entry,
        f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        b"\x01" + bytes.fromhex(DIMMER) + bytes.fromhex("4d641f0080232f00010201640000"),
    )
    await firmware(runtime, entry, 30048)
    await runtime.state(entry, DIMMER, "ON")
    await runtime.state(entry, DIMMER, "25", suffix="brightnessState")
    await runtime.receive(
        entry,
        f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusSynchronous",
        bytes.fromhex("1901640000" + DIMMER + "4d"),
    )
    await runtime.receive(
        entry,
        "lanbon/a/L8devElecsumReport",
        b"\x01\x01\x00" + bytes.fromhex(GROUP + DIMMER) + bytes(3),
    )
    for mode, raw in [
        ("normal", bytes(24)),
        ("delay", bytes(3)),
        ("greenMode", bytes(2)),
        ("nightMode", bytes.fromhex("0000150b0611")),
    ]:
        await runtime.receive(
            entry,
            f"lanbon/a/MAC:{GROUP}/s2m/cmdTimerGet/{mode}",
            raw + bytes.fromhex(DIMMER) + (bytes(8) if mode == "normal" else b""),
        )
    device = hub.store.devices[(DIMMER, None)]
    assert set(device.details) == {
        "inventory",
        "firmware",
        "network",
        "power",
        "brightness",
        "sync",
        "energy",
        "timer_normal",
        "timer_delay",
        "timer_greenMode",
        "timer_nightMode",
    }
    assert len(device.diagnostics) <= 64
    assert len(device.details["network"].value["fields"]["mesh_members"]) == 256
    required = {
        "timer_extension_raw",
        "root_normal_timer_running_candidate",
        "network_characteristics",
        "energy_decoder_candidate",
        "firmware_version",
    }
    assert required <= device.diagnostics.keys(), required - device.diagnostics.keys()
