"""Constants for the KDE Connect integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "kdeconnect"

CONF_DEVICE_ID = "device_id"
CONF_CERT = "cert_pem"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.LOCK,
    Platform.MEDIA_PLAYER,
    Platform.NOTIFY,
]

STORAGE_KEY_IDENTITY = f"{DOMAIN}.identity"
STORAGE_VERSION = 1

EVENT_NOTIFICATION_ACTION = f"{DOMAIN}_notification_action"
EVENT_NOTIFICATION = f"{DOMAIN}_notification"
EVENT_PING = f"{DOMAIN}_ping"
EVENT_COMMAND_OUTPUT = f"{DOMAIN}_command_output"
