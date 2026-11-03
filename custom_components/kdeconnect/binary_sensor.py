"""Connectivity sensor; doubles as desktop presence (kdeconnectd runs per session)."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity, async_add_when_battery_seen
from .protocol.device import Event


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([KdeConnectConnectedSensor(entry.runtime_data, "connected")])
    async_add_when_battery_seen(
        hass,
        entry,
        "binary_sensor",
        "battery_charging",
        lambda: [KdeConnectChargingSensor(entry.runtime_data, "battery_charging")],
        async_add_entities,
    )


class KdeConnectConnectedSensor(KdeConnectEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.device.connected and self.device.paired

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"host": self.device.host}


class KdeConnectChargingSensor(KdeConnectEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
    _events = frozenset({Event.BATTERY})

    @property
    def available(self) -> bool:
        return super().available and self.device.battery_charging is not None

    @property
    def is_on(self) -> bool | None:
        return self.device.battery_charging
