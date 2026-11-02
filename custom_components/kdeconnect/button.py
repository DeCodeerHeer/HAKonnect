"""Buttons: one per run command (defined on the desktop), plus ping and ring."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity
from .protocol.device import Event, RemoteDevice


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    device = entry.runtime_data
    async_add_entities(
        [
            KdeConnectActionButton(device, "ping", "ping", lambda d: d.ping()),
            KdeConnectActionButton(device, "ring", "ring", lambda d: d.ring()),
        ]
    )

    known: dict[str, KdeConnectCommandButton] = {}

    @callback
    def sync_commands(event: Event | None = None, _data=None) -> None:
        if event is not None and event is not Event.COMMANDS:
            return
        new = [KdeConnectCommandButton(device, key) for key in device.commands if key not in known]
        for button in new:
            known[button.command_key] = button
        if new:
            async_add_entities(new)
        if event is None:
            return  # nothing authoritative yet; the desktop hasn't sent its list
        # A received command list is authoritative; drop buttons for deleted commands.
        registry = er.async_get(hass)
        for key in [k for k in known if k not in device.commands]:
            button = known.pop(key)
            if button.entity_id and registry.async_get(button.entity_id):
                registry.async_remove(button.entity_id)

    # Re-create buttons for commands seen before a restart so they don't flap
    # to "unavailable then missing" while the desktop is offline.
    registry = er.async_get(hass)
    prefix = f"{device.device_id}_command_"
    restored = []
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "button" and reg_entry.unique_id.startswith(prefix):
            key = reg_entry.unique_id.removeprefix(prefix)
            known[key] = KdeConnectCommandButton(device, key, reg_entry.original_name)
            restored.append(known[key])
    if restored:
        async_add_entities(restored)

    sync_commands()
    entry.async_on_unload(device.add_listener(sync_commands))


class KdeConnectActionButton(KdeConnectEntity, ButtonEntity):
    def __init__(self, device: RemoteDevice, key: str, translation_key: str, press: Callable[[RemoteDevice], bool]) -> None:
        super().__init__(device, key)
        self._attr_translation_key = translation_key
        self._press = press

    async def async_press(self) -> None:
        self._press(self.device)


class KdeConnectCommandButton(KdeConnectEntity, ButtonEntity):
    _events = frozenset({Event.COMMANDS})

    def __init__(self, device: RemoteDevice, key: str, name: str | None = None) -> None:
        super().__init__(device, f"command_{key}")
        self.command_key = key
        self._fallback_name = name or key

    @property
    def name(self) -> str:
        cmd = self.device.commands.get(self.command_key)
        return cmd["name"] if cmd else self._fallback_name

    @property
    def available(self) -> bool:
        return super().available and self.command_key in self.device.commands

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        cmd = self.device.commands.get(self.command_key)
        return {"command": cmd["command"], "key": self.command_key} if cmd else {"key": self.command_key}

    async def async_press(self) -> None:
        self.device.run_command(self.command_key)
