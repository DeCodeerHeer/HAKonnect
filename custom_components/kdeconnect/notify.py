"""Desktop notifications from Home Assistant."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KdeConnectNotify(entry.runtime_data, "notify")])


class KdeConnectNotify(KdeConnectEntity, NotifyEntity):
    _attr_name = None  # notify.<device name>

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        if self.device.notify(title or "", message) is None:
            raise HomeAssistantError(f"{self.device.name} is not connected")
