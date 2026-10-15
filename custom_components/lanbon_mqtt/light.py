"""Qualified native dimmer controls; reports alone determine displayed state."""

from __future__ import annotations

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .policy import SourceKind


class LanbonActuatorBase(Entity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, hub, device, kind):
        self.hub, self.device = hub, device
        channel = "device" if device.channel is None else f"channel_{device.channel}"
        self._attr_unique_id = f"{device.device_id}_{channel}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Lanbon",
            name=f"Lanbon {device.device_id}",
        )

    async def async_added_to_hass(self):
        for field in ("power", "brightness"):
            self.async_on_remove(
                self.hub.async_add_listener(
                    self.async_write_ha_state, (self.device.device_id, self.device.channel, field)
                )
            )

    @property
    def available(self):
        return (
            self.hub.enable_controls
            and self.hub.store.connected
            and self.device.device_id not in self.hub.conflicts
            and not self.hub.suppressed(self.device.device_id)
            and (
                self.device.channel is None
                or self.hub.route_for(self.device.device_id, self.device.channel) is not None
            )
        )

    @property
    def is_on(self):
        observation = self.hub.observation(self.device, "power")
        return observation.value == "ON" if observation else None

    @property
    def assumed_state(self):
        observation = self.hub.observation(self.device, "power")
        return observation is None or observation.source is SourceKind.ROOT_CACHE

    @property
    def extra_state_attributes(self):
        observation = self.hub.observation(self.device, "power")
        return {
            "reported_state_source": observation.source.value if observation else None,
            "peer_observation_age_known": False,
            "physical_state_verified": False,
        }


class LanbonDimmer(LanbonActuatorBase, LightEntity):
    _attr_name = "Dimmer"
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(self, hub, device):
        super().__init__(hub, device, "light")

    @property
    def brightness(self):
        observation = self.hub.observation(self.device, "brightness")
        return round(float(observation.value) * 255 / 100) if observation else None

    async def async_turn_on(self, **kwargs):
        if ATTR_BRIGHTNESS in kwargs:
            requested = max(0, min(255, kwargs[ATTR_BRIGHTNESS]))
            percent = max(1, round(requested * 100 / 255)) if requested else 0
            await self.hub.async_command(self.device, "brightness", percent)
        else:
            remembered = self.hub.observation(self.device, "brightness")
            if remembered is not None and 0 < remembered.value <= 100:
                percent = max(1, min(100, round(remembered.value)))
                await self.hub.async_command(self.device, "brightness", percent)
            else:
                await self.hub.async_command(self.device, "power", "ON")

    async def async_turn_off(self, **kwargs):
        await self.hub.async_command(self.device, "power", "OFF")


async def async_setup_entry(hass, entry, async_add_entities):
    hub, added = entry.runtime_data, set()

    @callback
    def discover():
        entities = []
        if not hub.enable_controls:
            return
        for (peer, channel), device in tuple(hub.store.devices.items()):
            if (
                channel is not None
                or peer in added
                or peer in hub.conflicts
                or hub.device_type(peer) != 0x4D
                or hub.suppressed(peer)
            ):
                continue
            added.add(peer)
            entities.append(LanbonDimmer(hub, device))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(hub.async_add_listener(discover))
    discover()
