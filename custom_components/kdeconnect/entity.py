"""Base entity: availability follows the link, state is pushed by the device."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
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


@callback
def async_add_when_battery_seen(
    hass: HomeAssistant,
    entry: ConfigEntry[RemoteDevice],
    platform: str,
    key: str,
    entities: Callable[[], list[Entity]],
    async_add_entities: Callable[[list[Entity]], None],
) -> None:
    """Add battery entities once the desktop reports a battery (or did before).

    Desktops without a battery never send kdeconnect.battery, so they get no
    battery entities at all instead of permanently unknown ones.
    """
    device = entry.runtime_data
    known = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{device.device_id}_{key}")
    added = False

    @callback
    def maybe_add(event: Event | None = None, _data: Any = None) -> None:
        nonlocal added
        if added or (event is not None and event is not Event.BATTERY):
            return
        if known or device.battery_charge is not None:
            added = True
            async_add_entities(entities())

    maybe_add()
    if not added:
        entry.async_on_unload(device.add_listener(maybe_add))
