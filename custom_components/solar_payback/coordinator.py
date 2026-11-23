"""Reads the source sensors, runs the engine and stores the state.

Energy is integrated on every state change of a source sensor (and on a
10 s heartbeat): the previous readings are held until the change (left
Riemann sum), so no reading of the source integration is skipped.
Grid import/export can come from real kWh counters (P1 meter) instead.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
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
    GRID_SOURCE_COUNTERS,
    GRID_SOURCE_POWER,
    GRID_SOURCE_POWER_FALLBACK,
    MAX_COUNTER_STEP_KWH,
    MAX_GAP,
    NOTIFY_INTERVAL,
    POWER_SOURCES,
    PRICE_MAX_AGE,
    SAVE_DELAY_SECONDS,
    STORAGE_VERSION,
    TICK_INTERVAL,
)
from .engine import Engine, Inputs
from .rates import DEFAULT_SCHEDULE, RateSchedule, parse_schedule

_LOGGER = logging.getLogger(__name__)

_POWER_FACTORS = {"W": 1.0, "kW": 1000.0, "MW": 1_000_000.0}
_ENERGY_FACTORS = {"Wh": 0.001, "kWh": 1.0, "MWh": 1000.0}


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


class SolarPaybackCoordinator(DataUpdateCoordinator[None]):
    """Owns the engine; entities read from ``coordinator.engine``."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.engine = Engine()
        self._unsubs: list[CALLBACK_TYPE] = []
        # Held readings since ``_held_at`` (None = unavailable)
        self._held: dict[str, float | None] = {}
        self._held_price: float | None = None
        self._held_at: datetime | None = None
        self._last_price: float | None = None
        self._last_price_at: datetime | None = None
        self._counter_last: dict[str, float] = {}
        self._last_notify: datetime | None = None
        self.input_problem: str | None = None
        self.grid_source: str = GRID_SOURCE_POWER

        options = entry.options
        self._import_counters = _as_list(options.get(CONF_GRID_IMPORT_ENERGY))
        self._export_counters = _as_list(options.get(CONF_GRID_EXPORT_ENERGY))
        self._use_counters = bool(self._import_counters and self._export_counters)

    # ------------------------------------------------------------- lifecycle
    async def async_initialize(self) -> None:
        """Load stored counters and apply the configuration."""
        data = await self._store.async_load()
        self.engine = Engine(data)
        options = self.config_entry.options
        self.engine.schedule = RateSchedule(
            parse_schedule(options.get(CONF_RATE_SCHEDULE, DEFAULT_SCHEDULE))
        )
        self.engine.export_keeps_vat = bool(options.get(CONF_EXPORT_KEEPS_VAT, False))
        year = options.get(CONF_BATTERY_IN_USE_YEAR)
        self.engine.battery_in_use_year = int(year) if year else None
        self.engine.rollover(dt_util.now())

    @callback
    def async_start(self) -> None:
        """Start listening to the sources."""
        now = dt_util.utcnow()
        self._refresh_held(now)
        self._rebaseline_counters()
        options = self.config_entry.options
        watched = [options[k] for k in POWER_SOURCES if options.get(k)]
        watched += [options[CONF_SPOT_PRICE]] if options.get(CONF_SPOT_PRICE) else []
        watched += self._import_counters + self._export_counters
        self._unsubs.append(
            async_track_state_change_event(self.hass, watched, self._async_source_changed)
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._async_heartbeat, TICK_INTERVAL)
        )

    async def async_stop(self) -> None:
        """Stop listening and write the state to disk."""
        while self._unsubs:
            self._unsubs.pop()()
        await self._store.async_save(self.engine.to_dict())

    async def _async_update_data(self) -> None:
        """Not polled; updates are pushed."""
        return None

    @callback
    def async_save_soon(self) -> None:
        """Schedule a (debounced) write to disk."""
        self._store.async_delay_save(self.engine.to_dict, SAVE_DELAY_SECONDS)

    @callback
    def async_notify(self) -> None:
        """Push the current state to all entities."""
        self._last_notify = dt_util.utcnow()
        self.async_set_updated_data(None)

    # --------------------------------------------------------------- reading
    def _state_float(self, entity_id: str | None) -> tuple[float, str] | None:
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(state.state), str(state.attributes.get("unit_of_measurement", ""))
        except ValueError:
            return None

    def _read_power(self, key: str) -> float | None:
        reading = self._state_float(self.config_entry.options.get(key))
        if reading is None:
            return None
        value, unit = reading
        return max(value * _POWER_FACTORS.get(unit, 1.0), 0.0)

    def _read_energy(self, entity_id: str) -> float | None:
        reading = self._state_float(entity_id)
        if reading is None:
            return None
        value, unit = reading
        return value * _ENERGY_FACTORS.get(unit, 1.0)

    def _read_spot_price(self, now: datetime) -> float | None:
        """Spot price in EUR/kWh excl. VAT (last good value if briefly missing)."""
        options = self.config_entry.options
        reading = self._state_float(options.get(CONF_SPOT_PRICE))
        if reading is not None:
            value, unit = reading
            if "MWh" in unit:
                value /= 1000
            if options.get(CONF_SPOT_INCLUDES_VAT, True):
                value /= 1 + self.engine.schedule.at(dt_util.as_local(now).date()).vat
            self._last_price, self._last_price_at = value, now
            return value
        if self._last_price_at is not None and now - self._last_price_at <= PRICE_MAX_AGE:
            return self._last_price
        return None

    def _refresh_held(self, now: datetime) -> None:
        self._held = {key: self._read_power(key) for key in POWER_SOURCES}
        self._held_price = self._read_spot_price(now)
        self._held_at = now

    def _rebaseline_counters(self) -> None:
        self._counter_last = {}
        for entity_id in self._import_counters + self._export_counters:
            value = self._read_energy(entity_id)
            if value is not None:
                self._counter_last[entity_id] = value

    def _counter_deltas(self) -> tuple[float, float] | None:
        """kWh imported/exported since the last call, or None if unusable."""
        values: dict[str, float] = {}
        for entity_id in self._import_counters + self._export_counters:
            value = self._read_energy(entity_id)
            if value is None or entity_id not in self._counter_last:
                self._rebaseline_counters()
                return None
            values[entity_id] = value
        deltas = {eid: values[eid] - self._counter_last[eid] for eid in values}
        if any(d < 0 or d > MAX_COUNTER_STEP_KWH for d in deltas.values()):
            _LOGGER.warning("Ignoring implausible P1 counter step: %s", deltas)
            self._counter_last = values
            return None
        self._counter_last = values
        return (
            sum(deltas[e] for e in self._import_counters),
            sum(deltas[e] for e in self._export_counters),
        )

    # ----------------------------------------------------------- integration
    @callback
    def _integrate(self, now: datetime) -> None:
        """Integrate the held readings from ``_held_at`` to ``now``, then refresh."""
        start = self._held_at or now
        dt_seconds = max((now - start).total_seconds(), 0.0)
        local_now = dt_util.as_local(now)
        rolled = False

        if dt_seconds > MAX_GAP.total_seconds():
            # HA was busy or stopped: don't guess what happened meanwhile.
            self._rebaseline_counters()
            rolled = self.engine.rollover(local_now)
        else:
            missing = [key for key, value in self._held.items() if value is None]
            imp_kwh = exp_kwh = None
            if self._use_counters:
                deltas = self._counter_deltas()
                if deltas is not None:
                    imp_kwh, exp_kwh = deltas
                    self.grid_source = GRID_SOURCE_COUNTERS
                else:
                    self.grid_source = GRID_SOURCE_POWER_FALLBACK
            if missing:
                self.input_problem = f"unavailable: {', '.join(missing)}"
                rolled = self.engine.rollover(local_now)
            else:
                self.input_problem = None if self._held_price is not None else "no price"
                rolled = self.engine.step(
                    local_now,
                    dt_seconds / 3600,
                    Inputs(
                        battery_discharge_w=self._held[CONF_BATTERY_DISCHARGE],
                        grid_import_w=self._held[CONF_GRID_IMPORT],
                        grid_export_w=self._held[CONF_GRID_EXPORT],
                        load_w=self._held[CONF_HOUSE_LOAD],
                        spot_excl_vat=self._held_price,
                    ),
                    grid_import_kwh=imp_kwh,
                    grid_export_kwh=exp_kwh,
                )

        self._refresh_held(now)
        self.async_save_soon()
        if (
            rolled
            or self._last_notify is None
            or now - self._last_notify >= NOTIFY_INTERVAL
        ):
            self.async_notify()

    @callback
    def _async_source_changed(self, event: Event[EventStateChangedData]) -> None:
        self._integrate(dt_util.utcnow())

    @callback
    def _async_heartbeat(self, now: datetime) -> None:
        self._integrate(dt_util.utcnow())
