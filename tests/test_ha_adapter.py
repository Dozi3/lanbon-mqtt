"""Exercise observation invariants and the adapter with real HA imports."""

from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.lanbon_mqtt.config_flow import (
    normalize_group_id,
    normalize_prefix,
)
from custom_components.lanbon_mqtt.const import DOMAIN
from custom_components.lanbon_mqtt.hub import LanbonHub
from custom_components.lanbon_mqtt.observation import ObservationStore
from custom_components.lanbon_mqtt.sensor import (
    DESCRIPTIONS,
    LanbonObservationSensor,
    async_setup_entry,
)

GROUP = "AABBCCDDEEFF"
DEVICE = "112233445566"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
async def hass(tmp_path):
    """Actual HA core object without the Linux-only runner/pytest HA plugin."""
    instance = HomeAssistant(str(tmp_path))
    instance.config_entries = ConfigEntries(instance, {})
    yield instance
    await instance.async_stop(force=True)


def config_entry():
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Synthetic Lanbon topic group",
        data={},
        options={},
        source="user",
        unique_id=GROUP,
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


def event(kind, value, *, group=GROUP, device=DEVICE, channel=None):
    return {
        "kind": kind,
        "topic": f"homeassistant/{group}/light/{device}/{kind}",
        "group_id": group,
        "device_id": device,
        "channel": channel,
        "fields": {kind if kind != "status" else "output_code": value},
        "confidence": "structural" if kind == "status" else "observed",
        "raw": {"length": 1, "hex": "00", "sha256": "sample"},
    }


def message(topic, payload, *, retained=False):
    return ReceiveMessage(
        topic=topic,
        payload=payload,
        qos=0,
        retain=retained,
        subscribed_topic=topic,
        timestamp=NOW.timestamp(),
    )


def test_retained_bootstrap_stays_cache_and_cannot_replace_live_off():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    store.accept(event("power", "ON"), retained=True, received=NOW)
    device = store.devices[(DEVICE, None)]
    assert device.fields == {}
    assert device.last_received is None
    assert device.cache["power"].value == "ON"
    store.accept(event("power", "OFF"), retained=False, received=NOW + timedelta(seconds=1))
    store.accept(event("power", "ON"), retained=True, received=NOW + timedelta(seconds=2))
    assert device.fields["power"].value == "OFF"
    assert device.cache["power"].value == "ON"
    assert device.last_received == NOW + timedelta(seconds=1)


def test_power_brightness_and_freshness_are_independent():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    store.accept(event("brightness", 73), retained=False, received=NOW)
    store.accept(event("power", "OFF"), retained=False, received=NOW + timedelta(seconds=90))
    device = store.devices[(DEVICE, None)]
    assert device.fields["brightness"].value == 73
    assert device.fields["power"].value == "OFF"
    assert not store.available(device.fields["brightness"], NOW + timedelta(seconds=100), 60)
    assert store.available(device.fields["power"], NOW + timedelta(seconds=100), 60)


def test_disconnect_reconnect_requires_a_fresh_report_for_each_field():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    store.accept(event("power", "OFF"), retained=False, received=NOW)
    store.accept(event("brightness", 73), retained=False, received=NOW)
    device = store.devices[(DEVICE, None)]
    assert store.available(device.fields["power"], NOW, 300)
    store.set_connected(False)
    assert not store.available(device.fields["power"], NOW, 300)
    store.set_connected(True)
    store.accept(event("power", "ON"), retained=True, received=NOW)
    assert not store.available(device.fields["power"], NOW, 300)
    store.accept(event("brightness", 30), retained=False, received=NOW)
    assert not store.available(device.fields["power"], NOW, 300)
    assert store.available(device.fields["brightness"], NOW, 300)


def test_structural_native_zero_does_not_assign_generic_power_or_brightness():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    store.accept(event("status", 0), retained=False, received=NOW)
    device = store.devices[(DEVICE, None)]
    assert set(device.fields) == {"output_code"}
    assert device.fields["output_code"].value == 0


def test_group_attributed_inventory_discovers_structure_without_actuator_semantics():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    inventory = {
        "kind": "status_inventory",
        "group_id": GROUP,
        "topic": f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        "confidence": "structural",
        "devices": [
            {"device_id": DEVICE, "device_type": 0x4D, "model": "Dimmer", "status_raw": 73}
        ],
    }
    store.accept(inventory, retained=True, received=NOW)
    device = store.devices[(DEVICE, None)]
    assert device.fields == {}
    assert device.detail == {}
    assert device.metadata.cache["candidate_model"].retained
    assert device.cache["output_code"].value == 73
    store.accept(inventory, retained=False, received=NOW)
    assert set(device.fields) == {"output_code"}
    assert device.detail["candidate_model"] == "Dimmer"
    assert device.detail["device_type"] == 0x4D


def test_status_without_type_and_weaker_extension_preserve_verified_inventory_metadata():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    inventory = {
        "kind": "status_inventory",
        "group_id": GROUP,
        "topic": f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        "confidence": "structural",
        "devices": [
            {"device_id": DEVICE, "device_type": 0x4D, "model": "Dimmer", "status_raw": 73}
        ],
    }
    store.accept(inventory, retained=False, received=NOW)
    store.accept(event("status", 0), retained=False, received=NOW + timedelta(seconds=1))
    weaker = event("status", 0)
    weaker["fields"].update(
        {
            "candidate_device_type": 0x4A,
            "candidate_model": "One-gang switch",
            "marker_confidence": "live_extension_matches_firmware_type_enum",
        }
    )
    store.accept(weaker, retained=False, received=NOW + timedelta(seconds=2))
    device = store.devices[(DEVICE, None)]
    assert device.detail["device_type"] == 0x4D
    assert device.detail["candidate_model"] == "Dimmer"
    assert device.detail["model_confidence"] == "source_verified_type_enum"
    assert device.metadata.live["device_type"].received == NOW
    assert device.metadata.live["device_type"].topic == inventory["topic"]


def test_retained_detail_and_type_metadata_never_replace_live_metadata():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    detail = {
        "kind": "detail",
        "topic": "lanbon/a/L8devCompleteDetailReport",
        "confidence": "observed",
        "devices": [
            {
                "device_id": DEVICE,
                "firmware_raw": 30048,
                "firmware_version": "3.0.48",
                "firmware_confidence": "source_verified_encoding",
            }
        ],
    }
    store.accept(detail, retained=False, received=NOW)
    detail["devices"][0].update({"firmware_raw": 10113, "firmware_version": "1.1.13"})
    store.accept(detail, retained=True, received=NOW + timedelta(seconds=1))
    store.accept(event("power", "OFF"), retained=False, received=NOW + timedelta(seconds=2))
    device = store.devices[(DEVICE, None)]
    assert device.detail["firmware_version"] == "3.0.48"
    assert device.metadata.live["firmware_raw"].received == NOW
    assert not device.metadata.live["firmware_raw"].retained
    assert device.metadata.cache["firmware_version"].value == "1.1.13"
    assert device.metadata.cache["firmware_version"].retained
    live = event("status", 73)
    live["fields"].update(
        {
            "candidate_device_type": 0x4D,
            "candidate_model": "Dimmer",
            "marker_confidence": "live_extension_matches_firmware_type_enum",
        }
    )
    store.accept(live, retained=False, received=NOW + timedelta(seconds=3))
    replay = event("status", 0)
    replay["fields"].update(
        {
            "candidate_device_type": 0x4A,
            "candidate_model": "One-gang switch",
            "marker_confidence": "live_extension_matches_firmware_type_enum",
        }
    )
    store.accept(replay, retained=True, received=NOW + timedelta(seconds=4))
    assert device.detail["device_type"] == 0x4D
    assert device.metadata.cache["device_type"].value == 0x4A
    inventory_replay = {
        "kind": "status_inventory",
        "group_id": GROUP,
        "topic": f"lanbon/a/MAC:{GROUP}/s2m/cmdStatusGet",
        "confidence": "structural",
        "devices": [{"device_id": DEVICE, "device_type": 0x4B, "model": "Two-gang switch"}],
    }
    store.accept(inventory_replay, retained=True, received=NOW + timedelta(seconds=5))
    assert device.detail["device_type"] == 0x4D
    assert device.metadata.cache["device_type"].value == 0x4B


def test_unattributed_detail_cannot_create_or_assign_a_device():
    store = ObservationStore(GROUP)
    store.set_connected(True)
    detail = {
        "kind": "detail",
        "topic": "lanbon/a/L8devCompleteDetailReport",
        "devices": [{"device_id": DEVICE, "firmware_raw": 123, "firmware_version": "sample"}],
    }
    store.accept(detail, retained=False, received=NOW)
    assert store.devices == {}
    store.accept(event("power", "OFF", group="999999999999"), retained=False, received=NOW)
    assert store.devices == {}
    store.accept(event("power", "OFF"), retained=False, received=NOW)
    assert store.devices[(DEVICE, None)].detail["firmware_raw"] == 123


def test_channel_state_and_bounded_discovery_are_independent():
    store = ObservationStore(GROUP, max_devices=2)
    store.set_connected(True)
    store.accept(event("power", "ON", channel=1), retained=False, received=NOW)
    store.accept(event("power", "OFF", channel=2), retained=False, received=NOW)
    store.accept(event("power", "ON", channel=3), retained=False, received=NOW)
    assert store.devices[(DEVICE, 1)].fields["power"].value == "ON"
    assert store.devices[(DEVICE, 2)].fields["power"].value == "OFF"
    assert len(store.devices) == 2
    assert store.rejected_messages == 1


def test_group_and_prefix_validation_are_portable_and_literal():
    assert normalize_group_id("aa:bb:cc:dd:ee:ff") == GROUP
    assert normalize_group_id("aa-bb-cc-dd-ee-ff") == GROUP
    assert normalize_prefix(" /test/prefix/ ") == "test/prefix"
    for invalid in ["", "GG1122334455", "00112233445", "00112233445566"]:
        with pytest.raises(ValueError):
            normalize_group_id(invalid)
    for invalid in ["", "#", "home/+", "a//b", "a\x00b"]:
        with pytest.raises(ValueError):
            normalize_prefix(invalid)


async def test_actual_ha_mqtt_adapter_lifecycle_and_raw_messages(hass):
    hub = LanbonHub(hass, GROUP, "homeassistant", 300)
    subscriptions = []
    unsubscribers = []
    connection = {}
    timer_unsub = Mock()
    connection_unsub = Mock()

    async def subscribe(hass_arg, topic, callback, *, qos, encoding):
        assert hass_arg is hass
        assert encoding is None
        assert qos == 0
        subscriptions.append((topic, callback))
        unsubscribe = Mock()
        unsubscribers.append(unsubscribe)
        return unsubscribe

    def subscribe_connection(hass_arg, callback):
        connection["callback"] = callback
        return connection_unsub

    with (
        patch("custom_components.lanbon_mqtt.hub.mqtt.is_connected", return_value=True),
        patch("custom_components.lanbon_mqtt.hub.mqtt.async_subscribe", side_effect=subscribe),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_subscribe_connection_status",
            side_effect=subscribe_connection,
        ),
        patch(
            "custom_components.lanbon_mqtt.hub.async_track_time_interval", return_value=timer_unsub
        ),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_publish", new_callable=AsyncMock
        ) as publish,
    ):
        await hub.async_start()
        assert [topic for topic, _ in subscriptions] == [
            "lanbon/a/L8devCompleteDetailReport",
            f"lanbon/a/MAC:{GROUP}/#",
            f"homeassistant/{GROUP}/#",
        ]
        receiver = subscriptions[-1][1]
        topic = f"homeassistant/{GROUP}/light/{DEVICE}/state"
        receiver(message(topic, b"ON", retained=True))
        device = hub.store.devices[(DEVICE, None)]
        assert "power" not in device.fields
        receiver(message(topic, b"OFF"))
        assert device.fields["power"].value == "OFF"
        connection["callback"](False)
        connection["callback"](True)
        assert not hub.store.available(device.fields["power"], datetime.now(UTC), 300)
        receiver(message(topic, b"ON", retained=True))
        assert not hub.store.available(device.fields["power"], datetime.now(UTC), 300)
        receiver(message(topic, b"OFF"))
        assert hub.store.available(device.fields["power"], datetime.now(UTC), 300)
        hub.async_stop()
        count = hub.store.messages
        receiver(message(topic, b"ON"))
        assert hub.store.messages == count
        hub.async_stop()
        for unsubscribe in unsubscribers + [connection_unsub, timer_unsub]:
            unsubscribe.assert_called_once_with()
        publish.assert_not_called()


async def test_partial_subscription_failure_cleans_up(hass):
    hub = LanbonHub(hass, GROUP, "homeassistant", 300)
    first_unsubscribe = Mock()
    connection_unsubscribe = Mock()
    with (
        patch("custom_components.lanbon_mqtt.hub.mqtt.is_connected", return_value=True),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_subscribe",
            side_effect=[first_unsubscribe, RuntimeError("synthetic subscribe failure")],
        ),
        patch(
            "custom_components.lanbon_mqtt.hub.mqtt.async_subscribe_connection_status",
            return_value=connection_unsubscribe,
        ),
    ):
        with pytest.raises(RuntimeError, match="synthetic"):
            await hub.async_start()
    first_unsubscribe.assert_called_once_with()
    connection_unsubscribe.assert_called_once_with()
    assert not hub.store.connected


async def test_dynamic_diagnostic_sensors_use_actual_ha_classes(hass):
    hub = LanbonHub(hass, GROUP, "homeassistant", 300)
    hub.store.set_connected(True)
    entry = config_entry()
    entry.runtime_data = hub
    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    hub.store.accept(event("power", "OFF", channel=2), retained=False, received=NOW)
    hub._notify()
    assert len(added) == 1 + len(DESCRIPTIONS)
    hub._notify()
    assert len(added) == 1 + len(DESCRIPTIONS)
    power = next(sensor for sensor in added[1:] if sensor.entity_description.key == "power")
    detail = {
        "kind": "detail",
        "topic": "lanbon/a/L8devCompleteDetailReport",
        "devices": [
            {
                "device_id": DEVICE,
                "firmware_raw": 30048,
                "firmware_confidence": "source_verified_encoding",
            }
        ],
    }
    hub.store.accept(detail, retained=False, received=NOW)
    detail["devices"][0]["firmware_raw"] = 10113
    hub.store.accept(detail, retained=True, received=NOW)
    with patch("custom_components.lanbon_mqtt.sensor.dt_util.utcnow", return_value=NOW):
        assert power.available
        assert power.native_value == "OFF"
        assert power.extra_state_attributes["physical_state_verified"] is False
        assert power.unique_id.endswith("112233445566_channel_2_power")
        live_metadata = power.extra_state_attributes["metadata_provenance"]["firmware_raw"]
        cached_metadata = power.extra_state_attributes["cached_metadata"]["firmware_raw"]
        assert live_metadata["value"] == 30048
        assert live_metadata["capture_current"]
        assert live_metadata["retained"] is False
        assert live_metadata["source_topic"] == detail["topic"]
        assert live_metadata["received_utc"] == NOW.isoformat()
        assert cached_metadata["value"] == 10113
        assert cached_metadata["retained"] is True
        assert not cached_metadata["capture_current"]
    with patch(
        "custom_components.lanbon_mqtt.sensor.dt_util.utcnow",
        return_value=NOW + timedelta(seconds=301),
    ):
        assert not power.available
        assert not power.extra_state_attributes["metadata_provenance"]["firmware_raw"][
            "capture_current"
        ]
    assert isinstance(power, LanbonObservationSensor)
