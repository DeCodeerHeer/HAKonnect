"""KDE Connect protocol constants (verified against kdeconnect-kde 26.08)."""

from __future__ import annotations

PROTOCOL_VERSION = 8

UDP_PORT = 1716
MIN_TCP_PORT = 1716  # lanlinkprovider.h; 1714-1715 are rejected
MAX_TCP_PORT = 1764

# kdeconnect-kde DeviceInfo::isValidDeviceId
DEVICE_ID_PATTERN = r"^[a-zA-Z0-9_-]{32,38}$"

# lanlinkprovider.cpp: peer aborts if the in-TLS identity doesn't arrive within 1s.
IDENTITY_TIMEOUT = 1.0
MAX_IDENTITY_PACKET_SIZE = 8192
# landevicelink.cpp uses 512 KiB; mpris/notification packets are far smaller.
MAX_PACKET_SIZE = 512 * 1024

# pairinghandler.h
PAIR_TIMEOUT = 30.0

TYPE_IDENTITY = "kdeconnect.identity"
TYPE_PAIR = "kdeconnect.pair"
TYPE_PING = "kdeconnect.ping"
TYPE_MPRIS = "kdeconnect.mpris"
TYPE_MPRIS_REQUEST = "kdeconnect.mpris.request"
TYPE_RUNCOMMAND = "kdeconnect.runcommand"
TYPE_RUNCOMMAND_OUTPUT = "kdeconnect.runcommand.output"
TYPE_RUNCOMMAND_REQUEST = "kdeconnect.runcommand.request"
TYPE_LOCK = "kdeconnect.lock"
TYPE_LOCK_REQUEST = "kdeconnect.lock.request"
TYPE_NOTIFICATION = "kdeconnect.notification"
TYPE_NOTIFICATION_ACTION = "kdeconnect.notification.action"
TYPE_SHARE_REQUEST = "kdeconnect.share.request"
TYPE_SYSTEMVOLUME = "kdeconnect.systemvolume"
TYPE_SYSTEMVOLUME_REQUEST = "kdeconnect.systemvolume.request"
TYPE_FINDMYPHONE_REQUEST = "kdeconnect.findmyphone.request"

# What we accept from the desktop. The desktop drops any packet whose type
# isn't listed here (Device::sendPacket), so this gates what we can observe.
INCOMING_CAPABILITIES = [
    TYPE_PING,
    TYPE_MPRIS,
    TYPE_RUNCOMMAND,
    TYPE_RUNCOMMAND_OUTPUT,
    TYPE_LOCK,
    TYPE_NOTIFICATION,
    TYPE_NOTIFICATION_ACTION,
    TYPE_SYSTEMVOLUME,
]

# What we send. The desktop loads a plugin when our outgoing types intersect
# the plugin's supported types (PluginLoader::pluginsForCapabilities).
OUTGOING_CAPABILITIES = [
    TYPE_PING,
    TYPE_MPRIS_REQUEST,
    TYPE_RUNCOMMAND_REQUEST,
    TYPE_LOCK_REQUEST,
    TYPE_NOTIFICATION,
    TYPE_SHARE_REQUEST,
    TYPE_SYSTEMVOLUME_REQUEST,
    TYPE_FINDMYPHONE_REQUEST,
]
