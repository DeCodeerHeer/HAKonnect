"""A remote KDE Connect device: plugin state, commands and pairing."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .const import (
    PAIR_TIMEOUT,
    TYPE_FINDMYPHONE_REQUEST,
    TYPE_LOCK,
    TYPE_LOCK_REQUEST,
    TYPE_MPRIS,
    TYPE_MPRIS_REQUEST,
    TYPE_NOTIFICATION,
    TYPE_NOTIFICATION_ACTION,
    TYPE_PAIR,
    TYPE_PING,
    TYPE_RUNCOMMAND,
    TYPE_RUNCOMMAND_OUTPUT,
    TYPE_RUNCOMMAND_REQUEST,
    TYPE_SHARE_REQUEST,
    TYPE_SYSTEMVOLUME,
    TYPE_SYSTEMVOLUME_REQUEST,
)
from .link import Link
from .packets import Packet

_LOGGER = logging.getLogger(__name__)

_notification_ids = itertools.count(int(time.time()))


class PairingError(Exception):
    """Pairing was rejected, timed out or the link dropped."""


class Event(StrEnum):
    """Change notifications emitted to listeners."""

    CONNECTION = "connection"
    PLAYERS = "players"  # player list changed
    PLAYER = "player"  # one player's state changed; data = player name
    COMMANDS = "commands"
    COMMAND_OUTPUT = "command_output"  # data = packet body
    LOCK = "lock"
    SINKS = "sinks"
    PING = "ping"  # data = message
    NOTIFICATION_ACTION = "notification_action"  # data = {"key", "action"}
    NOTIFICATION = "notification"  # incoming desktop notification; data = body
    UNPAIRED = "unpaired"


@dataclass(slots=True)
class PlayerState:
    """Snapshot of one MPRIS player. Times are in milliseconds."""

    name: str
    title: str = ""
    artist: str = ""
    album: str = ""
    album_art_url: str = ""
    url: str = ""
    is_playing: bool = False
    pos: int = 0
    pos_updated: float = 0.0  # time.monotonic() when pos was received
    length: int = -1
    volume: int | None = None
    can_play: bool = False
    can_pause: bool = False
    can_go_next: bool = False
    can_go_previous: bool = False
    can_seek: bool = False
    loop_status: str | None = None
    shuffle: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    _FIELDS = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "albumArtUrl": "album_art_url",
        "url": "url",
        "isPlaying": "is_playing",
        "length": "length",
        "volume": "volume",
        "canPlay": "can_play",
        "canPause": "can_pause",
        "canGoNext": "can_go_next",
        "canGoPrevious": "can_go_previous",
        "canSeek": "can_seek",
        "loopStatus": "loop_status",
        "shuffle": "shuffle",
    }

    def update(self, body: dict[str, Any]) -> None:
        for key, attr in self._FIELDS.items():
            if key in body:
                setattr(self, attr, body[key])
        if "pos" in body:
            self.pos = int(body["pos"] or 0)
            self.pos_updated = time.monotonic()


Listener = Callable[[Event, Any], None]


class RemoteDevice:
    """State for one peer. Survives disconnects; the link comes and goes."""

    def __init__(self, device_id: str, name: str, cert_der: bytes | None, host: str | None) -> None:
        self.device_id = device_id
        self.name = name
        self.cert_der = cert_der  # pinned cert; None while unpaired
        self.host = host
        self.link: Link | None = None
        self.players: dict[str, PlayerState] = {}
        self.commands: dict[str, dict[str, str]] = {}
        self.locked: bool | None = None
        self.sinks: list[dict[str, Any]] = []
        self._listeners: list[Listener] = []
        self._pair_future: asyncio.Future[bool] | None = None

    def __repr__(self) -> str:
        return f"RemoteDevice({self.device_id!r}, {self.name!r}, connected={self.connected})"

    @property
    def connected(self) -> bool:
        return self.link is not None and not self.link.is_closed

    @property
    def paired(self) -> bool:
        return self.cert_der is not None

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _emit(self, event: Event, data: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, data)
            except Exception:
                _LOGGER.exception("Listener failed for %s", event)

    # -- link lifecycle ----------------------------------------------------

    def attach(self, link: Link) -> None:
        old, self.link = self.link, link
        if old is not None and old is not link:
            old.close()
        self.host = link.address
        self.name = link.identity.name
        self._emit(Event.CONNECTION, True)
        if self.paired:
            self.request_state()

    def detach(self, link: Link) -> None:
        if self.link is not link:
            return  # an older link that was already replaced
        self.link = None
        if self._pair_future and not self._pair_future.done():
            self._pair_future.set_exception(PairingError("link closed"))
        self._emit(Event.CONNECTION, False)

    def send(self, type_: str, body: dict[str, Any] | None = None) -> bool:
        if self.link is None:
            return False
        return self.link.send(Packet(type_, body or {}))

    def request_state(self) -> None:
        self.send(TYPE_MPRIS_REQUEST, {"requestPlayerList": True})
        self.send(TYPE_RUNCOMMAND_REQUEST, {"requestCommandList": True})
        self.send(TYPE_LOCK_REQUEST, {"requestLocked": True})
        self.send(TYPE_SYSTEMVOLUME_REQUEST, {"requestSinks": True})

    # -- pairing -----------------------------------------------------------

    async def request_pairing(self, on_code: Callable[[int], None] | None = None) -> bytes:
        """Ask the peer to pair; returns the peer cert to pin once accepted.

        on_code receives the timestamp so callers can compute the verification
        key shown in the desktop's notification.
        """
        if self.link is None:
            raise PairingError("not connected")
        timestamp = int(time.time())
        self._pair_future = asyncio.get_running_loop().create_future()
        if on_code:
            on_code(timestamp)
        self.link.send(Packet(TYPE_PAIR, {"pair": True, "timestamp": timestamp}))
        try:
            accepted = await asyncio.wait_for(self._pair_future, PAIR_TIMEOUT)
        except TimeoutError:
            self.send(TYPE_PAIR, {"pair": False})
            raise PairingError("timed out waiting for the desktop to accept") from None
        finally:
            self._pair_future = None
        if not accepted:
            raise PairingError("rejected or timed out on the desktop")
        assert self.link is not None
        self.cert_der = self.link.peer_cert_der
        return self.cert_der

    def unpair(self) -> None:
        self.send(TYPE_PAIR, {"pair": False})
        self.cert_der = None

    def _handle_pair(self, body: dict[str, Any]) -> None:
        wants = bool(body.get("pair"))
        if self._pair_future and not self._pair_future.done():
            # A request with a timestamp is the peer starting its own pairing at
            # the same time; treat it as consent, like PairingHandler does.
            self._pair_future.set_result(wants)
            return
        if wants:
            if not self.paired and "timestamp" in body:
                # Desktop-initiated pairing isn't supported; pair from HA.
                _LOGGER.info("Rejecting pairing request from %s; start pairing from Home Assistant", self.name)
                self.send(TYPE_PAIR, {"pair": False})
            return
        if self.paired:
            _LOGGER.warning("%s unpaired from Home Assistant", self.name)
            self.cert_der = None
            self._emit(Event.UNPAIRED)

    # -- incoming packets --------------------------------------------------

    def handle_packet(self, packet: Packet) -> None:
        _LOGGER.debug("<- %s %s", self.device_id, packet.type)
        body = packet.body
        if packet.type == TYPE_PAIR:
            self._handle_pair(body)
            return
        if not self.paired:
            return
        handler = _HANDLERS.get(packet.type)
        if handler:
            handler(self, body)

    def _on_mpris(self, body: dict[str, Any]) -> None:
        if "playerList" in body:
            names = [str(n) for n in body.get("playerList") or []]
            self.players = {n: self.players.get(n) or PlayerState(n) for n in names}
            self._emit(Event.PLAYERS)
            for name in names:
                self.send(TYPE_MPRIS_REQUEST, {"player": name, "requestNowPlaying": True, "requestVolume": True})
        name = body.get("player")
        if name is None or body.get("transferringAlbumArt"):
            return
        player = self.players.get(name)
        if player is None:
            player = self.players[name] = PlayerState(name)
            self._emit(Event.PLAYERS)
        player.update(body)
        self._emit(Event.PLAYER, name)

    def _on_runcommand(self, body: dict[str, Any]) -> None:
        raw = body.get("commandList")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            _LOGGER.warning("Unparseable commandList from %s", self.name)
            return
        self.commands = {
            str(k): {"name": str(v.get("name", k)), "command": str(v.get("command", ""))}
            for k, v in parsed.items()
            if isinstance(v, dict)
        }
        self._emit(Event.COMMANDS)

    def _on_runcommand_output(self, body: dict[str, Any]) -> None:
        self._emit(Event.COMMAND_OUTPUT, body)

    def _on_lock(self, body: dict[str, Any]) -> None:
        if "isLocked" in body:
            self.locked = bool(body["isLocked"])
            self._emit(Event.LOCK, self.locked)

    def _on_systemvolume(self, body: dict[str, Any]) -> None:
        if "sinkList" in body:
            sinks = body["sinkList"]
            if isinstance(sinks, str):
                sinks = json.loads(sinks)
            self.sinks = list(sinks or [])
        elif "name" in body:
            for sink in self.sinks:
                if sink.get("name") == body["name"]:
                    sink.update({k: v for k, v in body.items() if k != "name"})
        self._emit(Event.SINKS)

    def _on_ping(self, body: dict[str, Any]) -> None:
        self._emit(Event.PING, body.get("message"))

    def _on_notification_action(self, body: dict[str, Any]) -> None:
        self._emit(Event.NOTIFICATION_ACTION, {"key": body.get("key"), "action": body.get("action")})

    def _on_notification(self, body: dict[str, Any]) -> None:
        self._emit(Event.NOTIFICATION, body)

    # -- commands ----------------------------------------------------------

    def ping(self, message: str | None = None) -> bool:
        return self.send(TYPE_PING, {"message": message} if message else {})

    def media_action(self, player: str, action: str) -> bool:
        """action: Play, Pause, PlayPause, Stop, Next, Previous (MPRIS method names)."""
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "action": action})

    def media_set_volume(self, player: str, volume: int) -> bool:
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "setVolume": max(0, min(100, int(volume)))})

    def media_set_position(self, player: str, position_ms: int) -> bool:
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "SetPosition": int(position_ms)})

    def media_seek(self, player: str, offset_us: int) -> bool:
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "Seek": int(offset_us)})

    def media_set_shuffle(self, player: str, shuffle: bool) -> bool:
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "setShuffle": shuffle})

    def media_set_loop(self, player: str, loop_status: str) -> bool:
        """loop_status: None, Track, Playlist."""
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "setLoopStatus": loop_status})

    def media_refresh(self, player: str) -> bool:
        return self.send(TYPE_MPRIS_REQUEST, {"player": player, "requestNowPlaying": True, "requestVolume": True})

    def run_command(self, key: str) -> bool:
        return self.send(TYPE_RUNCOMMAND_REQUEST, {"key": key})

    def set_locked(self, locked: bool) -> bool:
        return self.send(TYPE_LOCK_REQUEST, {"setLocked": locked})

    def ring(self) -> bool:
        return self.send(TYPE_FINDMYPHONE_REQUEST, {})

    def share_url(self, url: str) -> bool:
        return self.send(TYPE_SHARE_REQUEST, {"url": url})

    def share_text(self, text: str) -> bool:
        return self.send(TYPE_SHARE_REQUEST, {"text": text})

    def set_sink(self, name: str, *, volume: int | None = None, muted: bool | None = None, enabled: bool | None = None) -> bool:
        body: dict[str, Any] = {"name": name}
        if volume is not None:
            body["volume"] = int(volume)
        if muted is not None:
            body["muted"] = muted
        if enabled is not None:
            body["enabled"] = enabled
        return self.send(TYPE_SYSTEMVOLUME_REQUEST, body)

    def notify(
        self,
        title: str,
        text: str,
        *,
        app_name: str = "Home Assistant",
        notification_id: str | None = None,
        actions: list[str] | None = None,
        silent: bool = False,
    ) -> str | None:
        nid = notification_id or f"ha-{next(_notification_ids)}"
        body: dict[str, Any] = {
            "id": nid,
            "appName": app_name,
            "title": title,
            "text": text,
            "ticker": f"{title}: {text}" if title else text,
            "isClearable": True,
            "silent": silent,
        }
        if actions:
            body["actions"] = actions
        return nid if self.send(TYPE_NOTIFICATION, body) else None

    def cancel_notification(self, notification_id: str) -> bool:
        return self.send(TYPE_NOTIFICATION, {"id": notification_id, "isCancel": True})


_HANDLERS: dict[str, Callable[[RemoteDevice, dict[str, Any]], None]] = {
    TYPE_MPRIS: RemoteDevice._on_mpris,
    TYPE_RUNCOMMAND: RemoteDevice._on_runcommand,
    TYPE_RUNCOMMAND_OUTPUT: RemoteDevice._on_runcommand_output,
    TYPE_LOCK: RemoteDevice._on_lock,
    TYPE_SYSTEMVOLUME: RemoteDevice._on_systemvolume,
    TYPE_PING: RemoteDevice._on_ping,
    TYPE_NOTIFICATION_ACTION: RemoteDevice._on_notification_action,
    TYPE_NOTIFICATION: RemoteDevice._on_notification,
}
