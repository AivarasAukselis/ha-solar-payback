"""Calculation engine (no Home Assistant imports, easy to unit test)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .const import (
    MODE_NET_METERING,
    MODE_NET_METERING_SURPLUS,
    MODE_NO_NET_METERING,
    MODE_NO_PRICE,
    SETTING_BATTERY_COST,
    SETTING_INVESTMENT,
    VAT_ADJUSTMENT_YEARS,
)
from .rates import DEFAULT_SCHEDULE, RateSchedule, export_price, import_price, parse_schedule

COUNTER_KEYS = (
    "battery_to_home_total",
    "battery_to_grid_total",
    "battery_to_home_year",
    "battery_to_grid_year",
    "grid_import_year",
    "grid_export_year",
    "savings_total",
    "savings_year",
    "savings_month",
    "savings_day",
    "savings_tracked",
    "cost_without_year",
    "cost_actual_year",
)
YEAR_KEYS = (
    "battery_to_home_year",
    "battery_to_grid_year",
    "grid_import_year",
    "grid_export_year",
    "savings_year",
    "cost_without_year",
    "cost_actual_year",
)


@dataclass
class Inputs:
    """One sample of the source sensors (W, and EUR/kWh excl. VAT)."""

    battery_discharge_w: float
    grid_import_w: float
    grid_export_w: float
    load_w: float
    spot_excl_vat: float | None


@dataclass
class LastStep:
    """Prices used in the most recent step."""

    import_price: float | None = None
    export_price: float | None = None
    mode: str = MODE_NO_PRICE


class Engine:
    """Integrates power into energy and money, with period rollovers."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        stored = data.get("counters", {})
        self.counters: dict[str, float] = {
            key: float(stored.get(key, 0.0)) for key in COUNTER_KEYS
        }
        self.year: int | None = data.get("year")
        self.month: int | None = data.get("month")
        self.day: str | None = data.get("day")
        self.tracking_since: str | None = data.get("tracking_since")
        self.previous_year: dict[str, Any] | None = data.get("previous_year")
        settings = data.get("settings", {})
        self.settings: dict[str, float] = {
            SETTING_INVESTMENT: float(settings.get(SETTING_INVESTMENT, 0.0)),
            SETTING_BATTERY_COST: float(settings.get(SETTING_BATTERY_COST, 0.0)),
        }
        # Configuration (set by the coordinator from the config entry).
        self.schedule = RateSchedule(parse_schedule(DEFAULT_SCHEDULE))
        self.export_keeps_vat = False
        self.battery_in_use_year: int | None = None
        self.last = LastStep()

    # ------------------------------------------------------------------ storage
    def to_dict(self) -> dict[str, Any]:
        """Serialize state for Home Assistant storage."""
        return {
            "counters": dict(self.counters),
            "year": self.year,
            "month": self.month,
            "day": self.day,
            "tracking_since": self.tracking_since,
            "previous_year": self.previous_year,
            "settings": dict(self.settings),
        }

    # --------------------------------------------------------------- rollovers
    def rollover(self, now: datetime) -> bool:
        """Reset day/month/year counters when the local date changes."""
        today = now.date()
        if self.tracking_since is None:
            self.tracking_since = now.isoformat()
        if self.year is None:
            self.year, self.month, self.day = today.year, today.month, today.isoformat()
            return False

        changed = False
        if today.year != self.year:
            pct = self.private_use_pct()
            self.previous_year = {
                "year": self.year,
                "private_use_pct": None if pct is None else round(pct, 2),
                "vat_on_private_use": round(self.vat_on_private_use(self.year), 2),
                "battery_to_home_kwh": round(self.counters["battery_to_home_year"], 3),
                "battery_to_grid_kwh": round(self.counters["battery_to_grid_year"], 3),
                "grid_import_kwh": round(self.counters["grid_import_year"], 3),
                "grid_export_kwh": round(self.counters["grid_export_year"], 3),
                "savings": round(self.counters["savings_year"], 2),
            }
            for key in YEAR_KEYS:
                self.counters[key] = 0.0
            self.counters["savings_month"] = 0.0
            self.counters["savings_day"] = 0.0
            changed = True
        elif today.month != self.month:
            self.counters["savings_month"] = 0.0
            self.counters["savings_day"] = 0.0
            changed = True
        elif today.isoformat() != self.day:
            self.counters["savings_day"] = 0.0
            changed = True

        self.year, self.month, self.day = today.year, today.month, today.isoformat()
        return changed

    # -------------------------------------------------------------------- step
    def step(
        self,
        now: datetime,
        dt_hours: float,
        inp: Inputs,
        grid_import_kwh: float | None = None,
        grid_export_kwh: float | None = None,
    ) -> bool:
        """Integrate one interval. Returns True on a rollover.

        Power values are held over ``dt_hours`` (left Riemann sum). When the
        grid energy for the interval is known from real kWh counters (P1
        meter), pass it in ``grid_import_kwh``/``grid_export_kwh``; it then
        replaces power x time for grid import/export and the money.
        """
        rolled = self.rollover(now)
        dt_hours = max(dt_hours, 0.0)
        use_counters = grid_import_kwh is not None and grid_export_kwh is not None
        if dt_hours == 0 and not (use_counters and (grid_import_kwh or grid_export_kwh)):
            return rolled

        c = self.counters
        discharge = max(inp.battery_discharge_w, 0.0) / 1000
        exp_kw = max(inp.grid_export_w, 0.0) / 1000
        load_kwh = max(inp.load_w, 0.0) / 1000 * dt_hours
        if use_counters:
            imp_kwh = max(grid_import_kwh, 0.0)
            exp_kwh = max(grid_export_kwh, 0.0)
        else:
            imp_kwh = max(inp.grid_import_w, 0.0) / 1000 * dt_hours
            exp_kwh = exp_kw * dt_hours

        # Battery split: solar feeds the house first, so export during
        # discharge is counted as battery-to-grid (up to the discharge power).
        # Needs simultaneous power readings, so always uses power.
        to_grid = min(discharge, exp_kw) * dt_hours
        to_home = discharge * dt_hours - to_grid
        c["battery_to_grid_total"] += to_grid
        c["battery_to_home_total"] += to_home
        c["battery_to_grid_year"] += to_grid
        c["battery_to_home_year"] += to_home

        rates = self.schedule.at(now.date())
        netting = rates.net_metering and c["grid_export_year"] < c["grid_import_year"]
        c["grid_import_year"] += imp_kwh
        c["grid_export_year"] += exp_kwh

        if not rates.net_metering:
            mode = MODE_NO_NET_METERING
        elif netting:
            mode = MODE_NET_METERING
        else:
            mode = MODE_NET_METERING_SURPLUS

        if inp.spot_excl_vat is None:
            self.last = LastStep(None, None, MODE_NO_PRICE)
            return rolled

        p_in = import_price(inp.spot_excl_vat, rates)
        p_out = export_price(inp.spot_excl_vat, rates, netting, self.export_keeps_vat)
        cost_without = load_kwh * p_in
        cost_actual = imp_kwh * p_in - exp_kwh * p_out
        saving = cost_without - cost_actual

        for key in ("savings_total", "savings_year", "savings_month", "savings_day", "savings_tracked"):
            c[key] += saving
        c["cost_without_year"] += cost_without
        c["cost_actual_year"] += cost_actual
        self.last = LastStep(p_in, p_out, mode)
        return rolled

    # ----------------------------------------------------------------- results
    def private_use_pct(self) -> float | None:
        """Share of battery output used by the house this year (%)."""
        home = self.counters["battery_to_home_year"]
        grid = self.counters["battery_to_grid_year"]
        total = home + grid
        if total <= 0:
            return None
        return home / total * 100

    def adjustment_year(self, year: int) -> int | None:
        """1..5 within the VAT adjustment period, else None."""
        if self.battery_in_use_year is None:
            return None
        idx = year - self.battery_in_use_year
        if 0 <= idx < VAT_ADJUSTMENT_YEARS:
            return idx + 1
        return None

    def vat_on_private_use(self, year: int) -> float:
        """VAT to report for private use: cost / 5 x private % x VAT rate."""
        if self.adjustment_year(year) is None:
            return 0.0
        pct = self.private_use_pct() if year == self.year else None
        if pct is None:
            return 0.0
        vat = self.schedule.at(date(year, 12, 31)).vat
        cost = self.settings[SETTING_BATTERY_COST]
        return cost / VAT_ADJUSTMENT_YEARS * pct / 100 * vat

    def payback_progress(self) -> float | None:
        inv = self.settings[SETTING_INVESTMENT]
        if inv <= 0:
            return None
        return self.counters["savings_total"] / inv * 100

    def payback_remaining(self) -> float | None:
        inv = self.settings[SETTING_INVESTMENT]
        if inv <= 0:
            return None
        return max(inv - self.counters["savings_total"], 0.0)

    def average_daily_savings(self, now: datetime) -> float | None:
        if self.tracking_since is None:
            return None
        days = (now - datetime.fromisoformat(self.tracking_since)).total_seconds() / 86400
        if days < 7:
            return None
        return self.counters["savings_tracked"] / days

    def payback_estimate(self, now: datetime) -> date | None:
        """Rough payback date, extrapolating the average daily saving so far."""
        remaining = self.payback_remaining()
        if remaining is None:
            return None
        if remaining <= 0:
            return now.date()
        avg = self.average_daily_savings(now)
        if avg is None or avg <= 0:
            return None
        days = remaining / avg
        if days > 365 * 60:
            return None
        return (now + timedelta(days=days)).date()
