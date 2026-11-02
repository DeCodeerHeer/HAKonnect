"""Home Assistant as a KDE Connect peer."""

from __future__ import annotations

import logging
import ssl

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_CERT,
    CONF_DEVICE_ID,
    DOMAIN,
    EVENT_COMMAND_OUTPUT,
    EVENT_NOTIFICATION,
    EVENT_NOTIFICATION_ACTION,
    EVENT_PING,
    PLATFORMS,
)
from .hub import async_get_node, async_stop_node
from .protocol.device import Event, RemoteDevice
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type KdeConnectConfigEntry = ConfigEntry[RemoteDevice]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KdeConnectConfigEntry) -> bool:
    node = await async_get_node(hass)
    data = entry.data
    dev = node.add_device(
        data[CONF_DEVICE_ID],
        data[CONF_NAME],
        ssl.PEM_cert_to_DER_cert(data[CONF_CERT]),
        data.get(CONF_HOST),
    )
    entry.runtime_data = dev

    @callback
    def on_event(event: Event, payload) -> None:
        base = {"device_id": dev.device_id, "name": dev.name, "config_entry_id": entry.entry_id}
        if event is Event.CONNECTION and payload and dev.host and dev.host != entry.data.get(CONF_HOST):
            # Persist the address so we can announce to it directly after a restart.
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_HOST: dev.host})
        elif event is Event.UNPAIRED:
            entry.async_start_reauth(hass)
        elif event is Event.NOTIFICATION_ACTION:
            hass.bus.async_fire(EVENT_NOTIFICATION_ACTION, {**base, **payload})
        elif event is Event.NOTIFICATION:
            hass.bus.async_fire(
                EVENT_NOTIFICATION,
                {**base, **{k: payload.get(k) for k in ("id", "appName", "title", "text", "ticker", "isCancel")}},
            )
        elif event is Event.PING:
            hass.bus.async_fire(EVENT_PING, {**base, "message": payload})
        elif event is Event.COMMAND_OUTPUT:
            hass.bus.async_fire(EVENT_COMMAND_OUTPUT, {**base, **payload})

    entry.async_on_unload(dev.add_listener(on_event))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: KdeConnectConfigEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if (node := hass.data.get(DOMAIN)) is not None:
        node.remove_device(entry.data[CONF_DEVICE_ID])
        if not node.devices and not hass.config_entries.flow.async_progress_by_handler(DOMAIN):
            await async_stop_node(hass)
    return True
