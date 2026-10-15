"""Explicit, bounded read-only parameter requests for qualified devices."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


class LanbonReadParametersButton(ButtonEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Read device parameters"
    _attr_icon = "mdi:download-circle-outline"

    def __init__(self, hub, device):
        self.hub, self.device = hub, device
        self._attr_unique_id = f"{device.device_id}_device_read_parameters"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            manufacturer="Lanbon",
            name=f"Lanbon {device.device_id}",
        )

    async def async_added_to_hass(self):
        self.async_on_remove(
            self.hub.async_add_listener(
                self.async_write_ha_state, (self.device.device_id, None, "parameter_read_status")
            )
        )

    @property
    def available(self):
        return self.hub.parameter_read_available(self.device)

    async def async_press(self):
        await self.hub.async_read_parameters(self.device)


async def async_setup_entry(hass, entry, async_add_entities):
    hub, added = entry.runtime_data, set()

    @callback
    def discover():
        entities = []
        if hub.legacy:
            return
        for (peer, channel), device in tuple(hub.store.devices.items()):
            if (
                channel is None
                and peer not in added
                and hub.device_type(peer) in {0x4A, 0x4B, 0x4C, 0x4D}
            ):
                added.add(peer)
                entities.append(LanbonReadParametersButton(hub, device))
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(hub.async_add_listener(discover))
    discover()
