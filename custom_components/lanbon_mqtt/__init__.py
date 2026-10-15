"""Installable observational Lanbon MQTT custom integration."""

from __future__ import annotations

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ALLOW_EXISTING_CONTROLS,
    CONF_DISCOVERY_PREFIX,
    CONF_ENABLE_CONTROLS,
    CONF_GROUP_ID,
    CONF_GROUP_IDS,
    CONF_MAX_REPORT_AGE,
    CONF_METADATA_CHILDREN,
    CONF_PROTOCOL_PROFILE,
    CONF_QUERY_INTERVAL,
    CONF_QUERY_ON_START,
    CONF_ROOT_BINDINGS,
    DEFAULT_PROTOCOL_PROFILE,
    PLATFORMS,
)
from .hub import LanbonHub

type LanbonConfigEntry = ConfigEntry[LanbonHub]


async def async_setup_entry(hass: HomeAssistant, entry: LanbonConfigEntry) -> bool:
    """Prepare diagnostic entities before retained subscriptions can deliver."""
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("The Home Assistant MQTT integration is not ready")
    config = dict(entry.data) | dict(entry.options)
    if CONF_GROUP_IDS in config and config.get(CONF_PROTOCOL_PROFILE) != DEFAULT_PROTOCOL_PROFILE:
        raise ConfigEntryError("Select and confirm a supported Lanbon MQTT protocol profile")
    hub = LanbonHub(
        hass,
        config.get(CONF_GROUP_IDS, config.get(CONF_GROUP_ID)),
        config[CONF_DISCOVERY_PREFIX],
        config[CONF_MAX_REPORT_AGE],
        enable_controls=config.get(CONF_ENABLE_CONTROLS, False),
        allow_existing_controls=config.get(CONF_ALLOW_EXISTING_CONTROLS, False),
        query_on_start=config.get(CONF_QUERY_ON_START, False),
        query_interval=config.get(CONF_QUERY_INTERVAL, 0),
        entry_id=entry.entry_id,
    )
    entry.runtime_data = hub
    entry.async_on_unload(hub.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    poller = None
    try:
        await hub.async_load_routes()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await hub.async_start()
        if bindings := config.get(CONF_ROOT_BINDINGS):
            from .network import RootMetadataPoller

            poller = RootMetadataPoller(
                hass, hub, bindings, include_children=config.get(CONF_METADATA_CHILDREN, False)
            )
            hub.network_status = poller.status
            entry.async_on_unload(poller.async_stop)
            hub._unsubscribers.append(poller.async_stop)
            poller.async_start()
    except BaseException:
        if poller is not None:
            poller.async_stop()
        hub.async_stop()
        await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        raise
    return True


async def _async_options_updated(hass: HomeAssistant, entry: LanbonConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LanbonConfigEntry) -> bool:
    """Unload entities then remove subscriptions and transport callbacks."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry.runtime_data.async_stop()
    await entry.runtime_data.async_flush_routes()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: LanbonConfigEntry) -> None:
    """Remove only this entry's validated route metadata after removal."""
    await Store(hass, 1, f"lanbon_mqtt.{entry.entry_id}.routes").async_remove()
