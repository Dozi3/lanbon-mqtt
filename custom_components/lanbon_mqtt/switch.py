"""Relay controls require independently inventoried types and observed routes."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback

from .light import LanbonActuatorBase


class LanbonRelay(LanbonActuatorBase, SwitchEntity):
    def __init__(self, hub, device):
        super().__init__(hub, device, "switch")
        self._attr_name = f"Relay {device.channel}"

    async def async_turn_on(self, **kwargs):
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
                channel is None
                or (peer, channel) in added
                or peer in hub.conflicts
                or hub.device_type(peer) not in {0x4A, 0x4B, 0x4C}
                or not hub.route_for(peer, channel)
                or hub.suppressed(peer)
            ):
                continue
            added.add((peer, channel))
            entities.append(LanbonRelay(hub, device))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(hub.async_add_listener(discover))
    discover()
