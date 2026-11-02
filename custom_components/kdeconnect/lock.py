"""Screen lock via the desktop's lockdevice plugin (logind Lock/Unlock)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity
from .protocol.device import Event


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KdeConnectLock(entry.runtime_data, "screen_lock")])


class KdeConnectLock(KdeConnectEntity, LockEntity):
    _attr_translation_key = "screen_lock"
    _events = frozenset({Event.LOCK})

    @property
    def is_locked(self) -> bool | None:
        return self.device.locked

    async def async_lock(self, **kwargs: Any) -> None:
        self.device.set_locked(True)

    async def async_unlock(self, **kwargs: Any) -> None:
        self.device.set_locked(False)
