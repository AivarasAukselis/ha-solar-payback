"""Dated rate schedule and price model (no Home Assistant imports).

A schedule is a list of periods. Each period starts on its ``from`` date and
only needs the fields that change; everything else is inherited from the
previous period. All money amounts are EUR per kWh EXCLUDING VAT, the way
the government and most suppliers publish them.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from typing import Any

FLOAT_FIELDS = ("energy_tax", "purchase_fee", "feed_in_fee", "vat")
BOOL_FIELDS = ("net_metering", "net_metering_includes_purchase_fee")


class RateScheduleError(ValueError):
    """Raised when a rate schedule is invalid."""


@dataclass(frozen=True)
class Rates:
    """Rates that apply from ``start`` until the next period."""

    start: date
    energy_tax: float = 0.0
    purchase_fee: float = 0.0
    feed_in_fee: float = 0.0
    vat: float = 0.21
    net_metering: bool = False
    net_metering_includes_purchase_fee: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        out: dict[str, Any] = {"from": self.start.isoformat()}
        for f in fields(self):
            if f.name != "start":
                out[f.name] = getattr(self, f.name)
        return out


# Dutch defaults. Energy tax excl. VAT: 2026 = EUR 0.09161 (0.11085 incl.),
# 2027 = EUR 0.08802 (0.1065 incl., corrected Belastingplan 2027).
# Net metering (salderingsregeling) ends on 1 January 2027.
DEFAULT_SCHEDULE: list[dict[str, Any]] = [
    {
        "from": "2026-01-01",
        "energy_tax": 0.09161,
        "purchase_fee": 0.0,
        "feed_in_fee": 0.0,
        "vat": 0.21,
        "net_metering": True,
        "net_metering_includes_purchase_fee": False,
    },
    {
        "from": "2027-01-01",
        "energy_tax": 0.08802,
        "net_metering": False,
    },
]


def _parse_date(value: Any, idx: int) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        # Frontend YAML editors may turn 2027-01-01 into a full timestamp.
        if len(text) > 10 and text[10] in "T ":
            text = text[:10]
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise RateScheduleError(
        f"Period {idx}: 'from' must be a date like 2027-01-01, got {value!r}"
    )


def _parse_float(key: str, value: Any, idx: int) -> float:
    if isinstance(value, bool):
        raise RateScheduleError(f"Period {idx}: '{key}' must be a number")
    try:
        number = float(str(value).replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError) as err:
        raise RateScheduleError(f"Period {idx}: '{key}' must be a number") from err
    if key == "vat" and not 0 <= number < 1:
        raise RateScheduleError(
            f"Period {idx}: 'vat' must be a fraction like 0.21 (not 21)"
        )
    if key == "energy_tax" and number < 0:
        raise RateScheduleError(f"Period {idx}: 'energy_tax' cannot be negative")
    if abs(number) > 5:
        raise RateScheduleError(
            f"Period {idx}: '{key}' = {number} looks too large; use EUR per kWh"
        )
    return number


def parse_schedule(raw: Any) -> list[Rates]:
    """Validate a raw schedule and return fully resolved periods (sorted)."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise RateScheduleError("The schedule must be a list with at least one period")

    parsed: list[tuple[date, dict[str, Any]]] = []
    allowed = {"from", *FLOAT_FIELDS, *BOOL_FIELDS}
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise RateScheduleError(f"Period {idx} must be a mapping of field: value")
        unknown = set(item) - allowed
        if unknown:
            raise RateScheduleError(
                f"Period {idx}: unknown field(s) {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )
        if "from" not in item:
            raise RateScheduleError(f"Period {idx} is missing 'from'")
        start = _parse_date(item["from"], idx)
        values: dict[str, Any] = {}
        for key in FLOAT_FIELDS:
            if key in item:
                values[key] = _parse_float(key, item[key], idx)
        for key in BOOL_FIELDS:
            if key in item:
                if not isinstance(item[key], bool):
                    raise RateScheduleError(f"Period {idx}: '{key}' must be true or false")
                values[key] = item[key]
        parsed.append((start, values))

    parsed.sort(key=lambda p: p[0])
    starts = [p[0] for p in parsed]
    if len(set(starts)) != len(starts):
        raise RateScheduleError("Two periods start on the same date")

    periods: list[Rates] = []
    current = Rates(start=parsed[0][0])
    for start, values in parsed:
        current = replace(current, start=start, **values)
        periods.append(current)
    return periods


def normalize_schedule(raw: Any) -> list[dict[str, Any]]:
    """Validate and return the user's schedule with ISO date strings (for storage)."""
    parse_schedule(raw)  # raises on error
    items = [raw] if isinstance(raw, dict) else raw
    out = []
    for item in items:
        clean = dict(item)
        clean["from"] = _parse_date(item["from"], 0).isoformat()
        out.append(clean)
    out.sort(key=lambda i: i["from"])
    return out


class RateSchedule:
    """Look up the rates for a given day."""

    def __init__(self, periods: list[Rates]) -> None:
        if not periods:
            raise RateScheduleError("Empty schedule")
        self.periods = sorted(periods, key=lambda p: p.start)
        self._starts = [p.start for p in self.periods]

    def at(self, day: date) -> Rates:
        """Rates for ``day``; before the first period the first one is used."""
        idx = bisect_right(self._starts, day) - 1
        return self.periods[max(idx, 0)]

    def next_change(self, day: date) -> Rates | None:
        """The first period starting after ``day``, if any."""
        idx = bisect_right(self._starts, day)
        return self.periods[idx] if idx < len(self.periods) else None


def import_price(spot_excl_vat: float, rates: Rates) -> float:
    """All-in price per kWh bought: (spot + purchase fee + energy tax) + VAT."""
    return (spot_excl_vat + rates.purchase_fee + rates.energy_tax) * (1 + rates.vat)


def export_price(
    spot_excl_vat: float,
    rates: Rates,
    netting_active: bool,
    export_keeps_vat: bool,
) -> float:
    """Value of one kWh fed into the grid.

    - Spot income: the supplier pays the spot price; you keep the VAT on it
      only when you are not VAT-registered (``export_keeps_vat``).
    - Feed-in fee (terugleverkosten) is charged on your bill incl. VAT.
    - While net metering applies, each netted kWh also avoids the energy tax
      (and optionally the purchase fee) incl. VAT on a kWh you bought.
    """
    spot_income = spot_excl_vat * (1 + rates.vat) if export_keeps_vat else spot_excl_vat
    value = spot_income - rates.feed_in_fee * (1 + rates.vat)
    if netting_active:
        netted = rates.energy_tax
        if rates.net_metering_includes_purchase_fee:
            netted += rates.purchase_fee
        value += netted * (1 + rates.vat)
    return value
