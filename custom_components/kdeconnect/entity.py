"""Base entity: availability follows the link, state is pushed by the device."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .protocol.device import Event, RemoteDevice


class KdeConnectEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    # Events (besides CONNECTION) that should trigger a state write.
    _events: frozenset[Event] = frozenset()

    def __init__(self, device: RemoteDevice, key: str) -> None:
        self.device = device
        self._attr_unique_id = f"{device.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.device_id)},
            name=device.name,
            manufacturer="KDE",
            model="KDE Connect",
        )

    @property
    def available(self) -> bool:
        return self.device.connected and self.device.paired

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.device.add_listener(self._handle_event))

    @callback
    def _handle_event(self, event: Event, data: Any) -> None:
        if event is Event.CONNECTION or event is Event.UNPAIRED or event in self._events:
            self.async_write_ha_state()
