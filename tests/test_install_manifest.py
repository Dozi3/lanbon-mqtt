"""A clean HA MQTT startup must accept this integration's discovery filters."""

import json
from pathlib import Path

import pytest
from homeassistant.components.mqtt.util import valid_subscribe_topic


def test_discovery_filters_are_native_mqtt_strings():
    manifest = json.loads(
        (Path(__file__).parents[1] / "custom_components/lanbon_mqtt/manifest.json").read_text()
    )
    assert manifest["config_flow"] is True
    assert manifest["single_config_entry"] is True
    assert manifest["dependencies"] == ["mqtt"]
    assert len(manifest["mqtt"]) == 2
    # MQTT startup forwards these values directly into its subscription layer;
    # dictionaries accepted by generic JSON parsing must never reach that API.
    for topic in manifest["mqtt"]:
        assert isinstance(topic, str)
        assert valid_subscribe_topic(topic) == topic


def test_native_mqtt_rejects_object_discovery_filters():
    with pytest.raises((ValueError, TypeError)):
        valid_subscribe_topic({"topic": "homeassistant/+/light/+/config"})
