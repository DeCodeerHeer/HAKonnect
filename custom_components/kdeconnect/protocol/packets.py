"""Packet model and newline-delimited JSON framing."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DEVICE_ID_PATTERN,
    INCOMING_CAPABILITIES,
    OUTGOING_CAPABILITIES,
    PROTOCOL_VERSION,
    TYPE_IDENTITY,
)

_DEVICE_ID_RE = re.compile(DEVICE_ID_PATTERN)
# kdeconnect-kde DeviceInfo::filterName
_NAME_INVALID_RE = re.compile(r"[\"',;:.!?()\[\]<>]")


class PacketError(ValueError):
    """Raised for malformed packets."""


@dataclass(slots=True)
class Packet:
    type: str
    body: dict[str, Any] = field(default_factory=dict)
    id: int = 0
    payload_size: int | None = None

    def serialize(self) -> bytes:
        obj: dict[str, Any] = {
            "id": self.id or int(time.time() * 1000),
            "type": self.type,
            "body": self.body,
        }
        return json.dumps(obj, separators=(",", ":")).encode() + b"\n"

    @classmethod
    def parse(cls, line: bytes) -> Packet:
        try:
            obj = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise PacketError(f"invalid JSON: {err}") from err
        if not isinstance(obj, dict) or not isinstance(obj.get("type"), str):
            raise PacketError("packet has no type")
        body = obj.get("body")
        if not isinstance(body, dict):
            body = {}
        return cls(
            type=obj["type"],
            body=body,
            id=int(obj.get("id") or 0),
            payload_size=obj.get("payloadSize"),
        )


def is_valid_device_id(device_id: object) -> bool:
    return isinstance(device_id, str) and bool(_DEVICE_ID_RE.match(device_id))


def filter_name(name: str) -> str:
    return _NAME_INVALID_RE.sub("", name)[:32]


@dataclass(slots=True, frozen=True)
class Identity:
    """Identity as exchanged inside TLS."""

    device_id: str
    name: str
    device_type: str = "unknown"
    protocol_version: int = PROTOCOL_VERSION
    incoming: tuple[str, ...] = ()
    outgoing: tuple[str, ...] = ()

    @classmethod
    def from_packet(cls, packet: Packet) -> Identity:
        b = packet.body
        if packet.type != TYPE_IDENTITY or not is_valid_device_id(b.get("deviceId")):
            raise PacketError("invalid identity packet")
        return cls(
            device_id=b["deviceId"],
            name=filter_name(str(b.get("deviceName") or "")) or b["deviceId"],
            device_type=str(b.get("deviceType") or "unknown"),
            protocol_version=int(b.get("protocolVersion") or 0),
            incoming=tuple(b.get("incomingCapabilities") or ()),
            outgoing=tuple(b.get("outgoingCapabilities") or ()),
        )


def udp_discovery_packet(device_id: str, name: str, tcp_port: int) -> Packet:
    """DeviceInfo::toUdpDiscoveryPacket."""
    return Packet(
        TYPE_IDENTITY,
        {
            "deviceId": device_id,
            "deviceName": name,
            "protocolVersion": PROTOCOL_VERSION,
            "tcpPort": tcp_port,
        },
    )


def full_identity_packet(device_id: str, name: str, device_type: str) -> Packet:
    """DeviceInfo::toIdentityPacket, sent inside TLS."""
    return Packet(
        TYPE_IDENTITY,
        {
            "deviceId": device_id,
            "deviceName": name,
            "deviceType": device_type,
            "protocolVersion": PROTOCOL_VERSION,
            "incomingCapabilities": INCOMING_CAPABILITIES,
            "outgoingCapabilities": OUTGOING_CAPABILITIES,
        },
    )
