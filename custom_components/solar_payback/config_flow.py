"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_DISCHARGE,
    CONF_BATTERY_IN_USE_YEAR,
    CONF_EXPORT_KEEPS_VAT,
    CONF_GRID_EXPORT,
    CONF_GRID_EXPORT_ENERGY,
    CONF_GRID_IMPORT,
    CONF_GRID_IMPORT_ENERGY,
    CONF_HOUSE_LOAD,
    CONF_RATE_SCHEDULE,
    CONF_SPOT_INCLUDES_VAT,
    CONF_SPOT_PRICE,
    DOMAIN,
    POWER_SOURCES,
)
from .rates import DEFAULT_SCHEDULE, RateScheduleError, normalize_schedule

_POWER = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_ENERGY_COUNTERS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="energy", multiple=True)
)


def _sources_schema() -> vol.Schema:
    return vol.Schema(
        {
            **{vol.Required(key): _POWER for key in POWER_SOURCES},
            vol.Optional(CONF_GRID_IMPORT_ENERGY): _ENERGY_COUNTERS,
            vol.Optional(CONF_GRID_EXPORT_ENERGY): _ENERGY_COUNTERS,
            vol.Required(CONF_SPOT_PRICE): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_SPOT_INCLUDES_VAT): selector.BooleanSelector(),
            vol.Required(CONF_EXPORT_KEEPS_VAT): selector.BooleanSelector(),
            vol.Required(CONF_BATTERY_IN_USE_YEAR): selector.NumberSelector(
                selector.NumberSelectorConfig(min=2015, max=2100, step=1, mode="box")
            ),
        }
    )


def _rates_schema() -> vol.Schema:
    return vol.Schema({vol.Required(CONF_RATE_SCHEDULE): selector.ObjectSelector()})


def _source_defaults() -> dict[str, Any]:
    return {
        CONF_SPOT_INCLUDES_VAT: True,
        CONF_EXPORT_KEEPS_VAT: False,
        CONF_BATTERY_IN_USE_YEAR: dt_util.now().year,
    }


def _clean_sources(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_BATTERY_IN_USE_YEAR] = int(data[CONF_BATTERY_IN_USE_YEAR])
    for key in (CONF_GRID_IMPORT_ENERGY, CONF_GRID_EXPORT_ENERGY):
        if not data.get(key):
            data.pop(key, None)
    return data


def _validate_sources(data: dict[str, Any]) -> dict[str, str]:
    has_import = bool(data.get(CONF_GRID_IMPORT_ENERGY))
    has_export = bool(data.get(CONF_GRID_EXPORT_ENERGY))
    if has_import != has_export:
        return {"base": "counters_pair"}
    return {}


class SolarPaybackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Solar & Battery Payback."""

    VERSION = 1

    def __init__(self) -> None:
        self._sources: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: pick the source sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean_sources(user_input)
            errors = _validate_sources(data)
            if not errors:
                self._sources = data
                return await self.async_step_rates()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _sources_schema(), user_input or _source_defaults()
            ),
            errors=errors,
        )

    async def async_step_rates(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: the dated rate schedule."""
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            try:
                schedule = normalize_schedule(user_input[CONF_RATE_SCHEDULE])
            except RateScheduleError as err:
                errors["base"] = "invalid_schedule"
                placeholders["error"] = str(err)
            else:
                return self.async_create_entry(
                    title="Solar & Battery",
                    data={},
                    options={**self._sources, CONF_RATE_SCHEDULE: schedule},
                )
        suggested = user_input or {CONF_RATE_SCHEDULE: DEFAULT_SCHEDULE}
        return self.async_show_form(
            step_id="rates",
            data_schema=self.add_suggested_values_to_schema(_rates_schema(), suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return SolarPaybackOptionsFlow()


class SolarPaybackOptionsFlow(OptionsFlow):
    """Change sensors, VAT settings or the rate schedule later."""

    def __init__(self) -> None:
        self._sources: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 1: source sensors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _clean_sources(user_input)
            errors = _validate_sources(data)
            if not errors:
                self._sources = data
                return await self.async_step_rates()
        current = user_input or {**_source_defaults(), **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(_sources_schema(), current),
            errors=errors,
        )

    async def async_step_rates(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Step 2: rate schedule."""
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            try:
                schedule = normalize_schedule(user_input[CONF_RATE_SCHEDULE])
            except RateScheduleError as err:
                errors["base"] = "invalid_schedule"
                placeholders["error"] = str(err)
            else:
                return self.async_create_entry(
                    data={**self._sources, CONF_RATE_SCHEDULE: schedule}
                )
        suggested = user_input or {
            CONF_RATE_SCHEDULE: self.config_entry.options.get(CONF_RATE_SCHEDULE, DEFAULT_SCHEDULE)
        }
        return self.async_show_form(
            step_id="rates",
            data_schema=self.add_suggested_values_to_schema(_rates_schema(), suggested),
            errors=errors,
            description_placeholders=placeholders,
        )
