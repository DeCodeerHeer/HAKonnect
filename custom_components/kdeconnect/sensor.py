"""Battery level of the desktop (laptops only; see async_add_when_battery_seen)."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import KdeConnectConfigEntry
from .entity import KdeConnectEntity, async_add_when_battery_seen
from .protocol.device import Event


async def async_setup_entry(
    hass: HomeAssistant, entry: KdeConnectConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_when_battery_seen(
        hass, entry, "sensor", "battery", lambda: [KdeConnectBatterySensor(entry.runtime_data, "battery")], async_add_entities
    )


class KdeConnectBatterySensor(KdeConnectEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _events = frozenset({Event.BATTERY})

    @property
    def available(self) -> bool:
        return super().available and self.device.battery_charge is not None

    @property
    def native_value(self) -> int | None:
        return self.device.battery_charge

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        return {"low": self.device.battery_low}
