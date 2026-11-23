"""Solar & Battery Payback: savings, payback and battery VAT private use."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, SERVICE_SET_COUNTER
from .coordinator import SolarPaybackCoordinator
from .engine import COUNTER_KEYS

PLATFORMS = [Platform.SENSOR, Platform.NUMBER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type SolarPaybackConfigEntry = ConfigEntry[SolarPaybackCoordinator]

SET_COUNTER_SCHEMA = vol.Schema(
    {
        vol.Required("counter"): vol.In(COUNTER_KEYS),
        vol.Required("value"): vol.Coerce(float),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> None | bool:
    """Register the services."""

    async def _set_counter(call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            coordinator: SolarPaybackCoordinator = entry.runtime_data
            coordinator.engine.counters[call.data["counter"]] = call.data["value"]
            coordinator.async_save_soon()
            coordinator.async_notify()

    hass.services.async_register(DOMAIN, SERVICE_SET_COUNTER, _set_counter, SET_COUNTER_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarPaybackConfigEntry) -> bool:
    """Set up from a config entry."""
    coordinator = SolarPaybackCoordinator(hass, entry)
    await coordinator.async_initialize()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: SolarPaybackConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SolarPaybackConfigEntry) -> bool:
    """Unload a config entry and save the counters."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded
