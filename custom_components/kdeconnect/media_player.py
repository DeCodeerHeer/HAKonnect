"""One media_player per desktop; MPRIS players are exposed as sources.

A single entity avoids entity churn as browser tabs and apps come and go. The
active source follows whatever starts playing unless one was picked explicitly.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity
from .protocol.device import Event, PlayerState

_REPEAT_TO_MPRIS = {RepeatMode.OFF: "None", RepeatMode.ONE: "Track", RepeatMode.ALL: "Playlist"}
_MPRIS_TO_REPEAT = {v: k for k, v in _REPEAT_TO_MPRIS.items()}


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KdeConnectMediaPlayer(entry.runtime_data, "media")])


class KdeConnectMediaPlayer(KdeConnectEntity, MediaPlayerEntity):
    _attr_name = None
    _attr_media_content_type = MediaType.MUSIC
    _attr_media_image_remotely_accessible = False
    _events = frozenset({Event.PLAYERS, Event.PLAYER})

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._selected: str | None = None  # explicit choice via select_source
        self._last_active: str | None = None

    @callback
    def _handle_event(self, event: Event, data: Any) -> None:
        if event is Event.PLAYER and data in self.device.players and self.device.players[data].is_playing:
            self._last_active = data
        super()._handle_event(event, data)

    @property
    def _player(self) -> PlayerState | None:
        players = self.device.players
        if self._selected in players:
            return players[self._selected]
        if (playing := next((p for p in players.values() if p.is_playing), None)) is not None:
            return playing
        if self._last_active in players:
            return players[self._last_active]
        return next(iter(players.values()), None)

    @property
    def state(self) -> MediaPlayerState:
        player = self._player
        if player is None:
            return MediaPlayerState.IDLE
        if player.is_playing:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.PAUSED if player.title else MediaPlayerState.IDLE

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = MediaPlayerEntityFeature.SELECT_SOURCE
        if (p := self._player) is None:
            return features
        features |= MediaPlayerEntityFeature.STOP
        if p.can_play:
            features |= MediaPlayerEntityFeature.PLAY
        if p.can_pause:
            features |= MediaPlayerEntityFeature.PAUSE
        if p.can_go_next:
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if p.can_go_previous:
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        if p.can_seek:
            features |= MediaPlayerEntityFeature.SEEK
        if p.volume is not None:
            features |= MediaPlayerEntityFeature.VOLUME_SET
        if p.shuffle is not None:
            features |= MediaPlayerEntityFeature.SHUFFLE_SET
        if p.loop_status is not None:
            features |= MediaPlayerEntityFeature.REPEAT_SET
        return features

    @property
    def source(self) -> str | None:
        return p.name if (p := self._player) else None

    @property
    def source_list(self) -> list[str]:
        return list(self.device.players)

    @property
    def media_title(self) -> str | None:
        return (p := self._player) and p.title or None

    @property
    def media_artist(self) -> str | None:
        return (p := self._player) and p.artist or None

    @property
    def media_album_name(self) -> str | None:
        return (p := self._player) and p.album or None

    @property
    def media_image_url(self) -> str | None:
        # file:// art URLs point at the desktop's disk and are useless here.
        if (p := self._player) and p.album_art_url.startswith(("http://", "https://")):
            return p.album_art_url
        return None

    @property
    def media_duration(self) -> int | None:
        return p.length // 1000 if (p := self._player) and p.length > 0 else None

    @property
    def media_position(self) -> int | None:
        return p.pos // 1000 if (p := self._player) and p.pos_updated else None

    @property
    def media_position_updated_at(self) -> datetime | None:
        if not (p := self._player) or not p.pos_updated:
            return None
        return dt_util.utcnow() - timedelta(seconds=time.monotonic() - p.pos_updated)

    @property
    def volume_level(self) -> float | None:
        return p.volume / 100 if (p := self._player) and p.volume is not None else None

    @property
    def shuffle(self) -> bool | None:
        return p.shuffle if (p := self._player) else None

    @property
    def repeat(self) -> RepeatMode | None:
        return _MPRIS_TO_REPEAT.get(p.loop_status) if (p := self._player) and p.loop_status else None

    def _action(self, action: str) -> None:
        if p := self._player:
            self.device.media_action(p.name, action)

    async def async_media_play(self) -> None:
        self._action("Play")

    async def async_media_pause(self) -> None:
        self._action("Pause")

    async def async_media_stop(self) -> None:
        self._action("Stop")

    async def async_media_next_track(self) -> None:
        self._action("Next")

    async def async_media_previous_track(self) -> None:
        self._action("Previous")

    async def async_media_seek(self, position: float) -> None:
        if p := self._player:
            self.device.media_set_position(p.name, int(position * 1000))

    async def async_set_volume_level(self, volume: float) -> None:
        if p := self._player:
            self.device.media_set_volume(p.name, round(volume * 100))

    async def async_set_shuffle(self, shuffle: bool) -> None:
        if p := self._player:
            self.device.media_set_shuffle(p.name, shuffle)

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        if p := self._player:
            self.device.media_set_loop(p.name, _REPEAT_TO_MPRIS[repeat])

    async def async_select_source(self, source: str) -> None:
        self._selected = source
        self.device.media_refresh(source)
        self.async_write_ha_state()
