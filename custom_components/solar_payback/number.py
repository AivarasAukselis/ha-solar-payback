"""Editable settings: investment and battery cost."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SolarPaybackConfigEntry
from .const import SETTING_BATTERY_COST, SETTING_INVESTMENT
from .coordinator import SolarPaybackCoordinator
from .entity import SolarPaybackEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarPaybackConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number entities."""
    coordinator = entry.runtime_data
    async_add_entities(
        SettingNumber(coordinator, key) for key in (SETTING_INVESTMENT, SETTING_BATTERY_COST)
    )


class SettingNumber(SolarPaybackEntity, NumberEntity):
    """A euro amount stored with the counters."""

    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 250_000
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "EUR"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SolarPaybackCoordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._key = key

    @property
    def native_value(self) -> float:
        return self.coordinator.engine.settings[self._key]

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.engine.settings[self._key] = float(value)
        self.coordinator.async_save_soon()
        self.coordinator.async_notify()
