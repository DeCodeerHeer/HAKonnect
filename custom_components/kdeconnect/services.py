"""Integration-level actions targeting a paired device by config entry."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .protocol.device import RemoteDevice

ATTR_CONFIG_ENTRY = "config_entry_id"

_BASE = {vol.Required(ATTR_CONFIG_ENTRY): cv.string}


def _device(hass: HomeAssistant, call: ServiceCall) -> RemoteDevice:
    entry = hass.config_entries.async_get_entry(call.data[ATTR_CONFIG_ENTRY])
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError("Unknown KDE Connect config entry")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(f"{entry.title} is not loaded")
    dev: RemoteDevice = entry.runtime_data
    if not dev.connected:
        raise HomeAssistantError(f"{entry.title} is not connected")
    return dev


def _check(sent: bool) -> None:
    if not sent:
        raise HomeAssistantError("Failed to send to the device")


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    async def ping(call: ServiceCall) -> None:
        _check(_device(hass, call).ping(call.data.get("message")))

    async def share_url(call: ServiceCall) -> None:
        _check(_device(hass, call).share_url(call.data["url"]))

    async def share_text(call: ServiceCall) -> None:
        _check(_device(hass, call).share_text(call.data["text"]))

    async def run_command(call: ServiceCall) -> None:
        dev = _device(hass, call)
        key = call.data["key"]
        if key not in dev.commands:
            # Allow passing the display name as well as the key.
            key = next((k for k, c in dev.commands.items() if c["name"] == key), key)
        if key not in dev.commands:
            raise ServiceValidationError(f"Unknown command {call.data['key']!r}")
        _check(dev.run_command(key))

    async def send_notification(call: ServiceCall) -> ServiceResponse:
        nid = _device(hass, call).notify(
            call.data.get("title", ""),
            call.data["message"],
            app_name=call.data.get("app_name", "Home Assistant"),
            notification_id=call.data.get("id"),
            actions=call.data.get("actions"),
            silent=call.data.get("silent", False),
        )
        if nid is None:
            raise HomeAssistantError("Failed to send to the device")
        return {"id": nid}

    async def cancel_notification(call: ServiceCall) -> None:
        _check(_device(hass, call).cancel_notification(call.data["id"]))

    hass.services.async_register(DOMAIN, "ping", ping, vol.Schema({**_BASE, vol.Optional("message"): cv.string}))
    hass.services.async_register(DOMAIN, "share_url", share_url, vol.Schema({**_BASE, vol.Required("url"): cv.url}))
    hass.services.async_register(DOMAIN, "share_text", share_text, vol.Schema({**_BASE, vol.Required("text"): cv.string}))
    hass.services.async_register(
        DOMAIN, "run_command", run_command, vol.Schema({**_BASE, vol.Required("key"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        "send_notification",
        send_notification,
        vol.Schema(
            {
                **_BASE,
                vol.Required("message"): cv.string,
                vol.Optional("title"): cv.string,
                vol.Optional("app_name"): cv.string,
                vol.Optional("id"): cv.string,
                vol.Optional("actions"): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional("silent"): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN, "cancel_notification", cancel_notification, vol.Schema({**_BASE, vol.Required("id"): cv.string})
    )
