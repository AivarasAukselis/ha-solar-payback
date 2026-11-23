"""Sensors for Solar & Battery Payback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SolarPaybackConfigEntry
from .const import PRICING_MODES
from .coordinator import SolarPaybackCoordinator
from .engine import Engine
from .entity import SolarPaybackEntity

EUR = "EUR"
EUR_PER_KWH = "EUR/kWh"


def _start_of_day() -> datetime:
    return dt_util.start_of_local_day()


def _start_of_month() -> datetime:
    today = dt_util.now().date()
    return dt_util.start_of_local_day(date(today.year, today.month, 1))


def _start_of_year() -> datetime:
    return dt_util.start_of_local_day(date(dt_util.now().year, 1, 1))


def _counter(key: str, digits: int) -> Callable[[SolarPaybackCoordinator], float]:
    return lambda c: round(c.engine.counters[key], digits)


def _opt_round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _rates_attrs(c: SolarPaybackCoordinator) -> dict[str, Any]:
    engine: Engine = c.engine
    today = dt_util.now().date()
    rates = engine.schedule.at(today)
    nxt = engine.schedule.next_change(today)
    attrs: dict[str, Any] = {
        "rates_from": rates.start.isoformat(),
        "energy_tax_excl_vat": rates.energy_tax,
        "purchase_fee_excl_vat": rates.purchase_fee,
        "feed_in_fee_excl_vat": rates.feed_in_fee,
        "vat": rates.vat,
        "net_metering": rates.net_metering,
        "next_rate_change": nxt.as_dict() if nxt else None,
        "input_problem": c.input_problem,
        "grid_energy_source": c.grid_source,
    }
    return attrs


def _previous_year_attrs(c: SolarPaybackCoordinator) -> dict[str, Any]:
    return {"previous_year": c.engine.previous_year}


def _vat_attrs(c: SolarPaybackCoordinator) -> dict[str, Any]:
    engine = c.engine
    year = engine.year or dt_util.now().year
    prev = engine.previous_year or {}
    return {
        "adjustment_year": engine.adjustment_year(year),
        "battery_in_use_year": engine.battery_in_use_year,
        "previous_year": prev.get("year"),
        "previous_year_vat": prev.get("vat_on_private_use"),
        "report_in": "row 1d (Privégebruik), last VAT return of the year",
    }


def _payback_attrs(c: SolarPaybackCoordinator) -> dict[str, Any]:
    avg = c.engine.average_daily_savings(dt_util.now())
    return {
        "average_daily_savings": _opt_round(avg, 2),
        "tracking_since": c.engine.tracking_since,
        "note": "Extrapolates the average so far; seasonal, so rough",
    }


@dataclass(frozen=True, kw_only=True)
class PaybackSensorDescription(SensorEntityDescription):
    """Sensor description with value and attribute callbacks."""

    value_fn: Callable[[SolarPaybackCoordinator], Any]
    attrs_fn: Callable[[SolarPaybackCoordinator], dict[str, Any]] | None = None
    last_reset_fn: Callable[[], datetime] | None = None


def _money(key: str, counter: str, reset: Callable[[], datetime] | None) -> PaybackSensorDescription:
    return PaybackSensorDescription(
        key=key,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=_counter(counter, 2),
        last_reset_fn=reset,
    )


def _energy(
    key: str, counter: str, reset: Callable[[], datetime] | None, increasing: bool = False
) -> PaybackSensorDescription:
    return PaybackSensorDescription(
        key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING if increasing else SensorStateClass.TOTAL,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=_counter(counter, 3),
        last_reset_fn=reset,
    )


SENSORS: tuple[PaybackSensorDescription, ...] = (
    # Savings
    _money("savings_today", "savings_day", _start_of_day),
    _money("savings_this_month", "savings_month", _start_of_month),
    _money("savings_this_year", "savings_year", _start_of_year),
    _money("savings_total", "savings_total", None),
    _money("cost_without_system_this_year", "cost_without_year", _start_of_year),
    _money("actual_cost_this_year", "cost_actual_year", _start_of_year),
    # Payback
    PaybackSensorDescription(
        key="payback_progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: _opt_round(c.engine.payback_progress(), 2),
    ),
    PaybackSensorDescription(
        key="payback_remaining",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=0,
        value_fn=lambda c: _opt_round(c.engine.payback_remaining(), 2),
    ),
    PaybackSensorDescription(
        key="payback_estimated_date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda c: c.engine.payback_estimate(dt_util.now()),
        attrs_fn=_payback_attrs,
    ),
    # Battery VAT
    PaybackSensorDescription(
        key="battery_private_use_this_year",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c: _opt_round(c.engine.private_use_pct(), 2),
        attrs_fn=_previous_year_attrs,
    ),
    PaybackSensorDescription(
        key="vat_on_private_use_this_year",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=EUR,
        suggested_display_precision=2,
        value_fn=lambda c: round(c.engine.vat_on_private_use(c.engine.year or dt_util.now().year), 2),
        attrs_fn=_vat_attrs,
    ),
    _energy("battery_to_home_this_year", "battery_to_home_year", _start_of_year),
    _energy("battery_to_grid_this_year", "battery_to_grid_year", _start_of_year),
    _energy("battery_to_home_total", "battery_to_home_total", None, increasing=True),
    _energy("battery_to_grid_total", "battery_to_grid_total", None, increasing=True),
    _energy("grid_import_this_year", "grid_import_year", _start_of_year),
    _energy("grid_export_this_year", "grid_export_year", _start_of_year),
    # Prices
    PaybackSensorDescription(
        key="import_price",
        native_unit_of_measurement=EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda c: _opt_round(c.engine.last.import_price, 5),
    ),
    PaybackSensorDescription(
        key="export_value",
        native_unit_of_measurement=EUR_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda c: _opt_round(c.engine.last.export_price, 5),
    ),
    PaybackSensorDescription(
        key="pricing_mode",
        device_class=SensorDeviceClass.ENUM,
        options=PRICING_MODES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.engine.last.mode,
        attrs_fn=_rates_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarPaybackConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(PaybackSensor(coordinator, desc) for desc in SENSORS)


class PaybackSensor(SolarPaybackEntity, SensorEntity):
    """A value computed by the engine."""

    entity_description: PaybackSensorDescription

    def __init__(self, coordinator: SolarPaybackCoordinator, description: PaybackSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None

    @property
    def last_reset(self) -> datetime | None:
        fn = self.entity_description.last_reset_fn
        return fn() if fn else None
