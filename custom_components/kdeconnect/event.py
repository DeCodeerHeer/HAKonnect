"""Notifications forwarded by the desktop ("Send notifications" plugin)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity
from .protocol.device import Event

ATTRS = {"appName": "app_name", "title": "title", "text": "text", "ticker": "ticker", "id": "notification_id"}


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KdeConnectNotificationEvent(entry.runtime_data, "notification_received")])


class KdeConnectNotificationEvent(KdeConnectEntity, EventEntity):
    _attr_translation_key = "notification"
    _attr_event_types = ["received"]

    @callback
    def _handle_event(self, event: Event, data: Any) -> None:
        if event is Event.NOTIFICATION and not data.get("isCancel"):
            self._trigger_event("received", {attr: data.get(key) for key, attr in ATTRS.items() if key in data})
            self.async_write_ha_state()
            return
        super()._handle_event(event, data)
