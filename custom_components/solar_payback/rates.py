"""Dated rate schedule and price model (no Home Assistant imports).

A schedule is a list of periods. Each period starts on its ``from`` date and
only needs the fields that change; everything else is inherited from the
previous period. All money amounts are EUR per kWh EXCLUDING VAT, the way
the government and most suppliers publish them.

Feed-in bonuses (e.g. Zonneplan Zonnebonus, NextEnergy Zonnebonus, Frank
Energie Terugleverbonus) are a list of rules per period, each with a
percentage, a basis, a daily time window and an optional yearly kWh cap.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time
from typing import Any

FLOAT_FIELDS = ("energy_tax", "purchase_fee", "feed_in_fee", "feed_in_credit", "vat")
BOOL_FIELDS = ("net_metering", "net_metering_includes_purchase_fee")
LIST_FIELDS = ("feed_in_bonus",)

BONUS_BASIS_MARKET = "market"
BONUS_BASIS_MARKET_PLUS_CREDIT = "market_plus_credit"
BONUS_BASES = (BONUS_BASIS_MARKET, BONUS_BASIS_MARKET_PLUS_CREDIT)
SUN_EVENTS = ("sunrise", "sunset")
BONUS_FIELDS = ("name", "percent", "of", "from", "until", "max_kwh_per_year", "only_positive")


class RateScheduleError(ValueError):
    """Raised when a rate schedule is invalid."""


@dataclass(frozen=True)
class BonusRule:
    """Extra feed-in compensation as a percentage of the market price."""

    percent: float
    of: str = BONUS_BASIS_MARKET
    start: str = "00:00"  # "HH:MM", "sunrise" or "sunset"
    end: str = "24:00"
    max_kwh_per_year: float | None = None
    only_positive: bool = True
    name: str | None = None

    @property
    def key(self) -> str:
        """Stable identity for the yearly kWh cap (survives inherited periods)."""
        if self.name:
            return self.name
        return f"{self.percent}|{self.of}|{self.start}|{self.end}|{self.max_kwh_per_year}"

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "percent": self.percent,
            "of": self.of,
            "from": self.start,
            "until": self.end,
            "only_positive": self.only_positive,
        }
        if self.name:
            out["name"] = self.name
        if self.max_kwh_per_year is not None:
            out["max_kwh_per_year"] = self.max_kwh_per_year
        return out


@dataclass(frozen=True)
class Rates:
    """Rates that apply from ``start`` until the next period."""

    start: date
    energy_tax: float = 0.0
    purchase_fee: float = 0.0
    feed_in_fee: float = 0.0
    feed_in_credit: float = 0.0
    vat: float = 0.21
    net_metering: bool = False
    net_metering_includes_purchase_fee: bool = False
    feed_in_bonus: tuple[BonusRule, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict."""
        out: dict[str, Any] = {"from": self.start.isoformat()}
        for f in fields(self):
            if f.name == "start":
                continue
            value = getattr(self, f.name)
            if f.name == "feed_in_bonus":
                value = [rule.as_dict() for rule in value]
            out[f.name] = value
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


def _to_number(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        return float(value.replace(",", "."))
    return float(value)


def _parse_float(key: str, value: Any, idx: int) -> float:
    try:
        number = _to_number(value)
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


def _parse_clock(value: Any, where: str) -> str:
    """Accept 'HH:MM', 'sunrise', 'sunset' (also YAML-parsed sexagesimal ints)."""
    if isinstance(value, int) and not isinstance(value, bool):
        # YAML 1.1 reads an unquoted 06:00 as 360 (minutes); accept that.
        if 0 <= value <= 1440:
            return f"{value // 60:02d}:{value % 60:02d}"
    if isinstance(value, time):
        return value.strftime("%H:%M")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in SUN_EVENTS:
            return text
        parts = text.split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            hours, minutes = int(parts[0]), int(parts[1])
            if (0 <= hours < 24 and 0 <= minutes < 60) or (hours == 24 and minutes == 0):
                return f"{hours:02d}:{minutes:02d}"
    raise RateScheduleError(
        f"{where}: time must be 'HH:MM', 'sunrise' or 'sunset', got {value!r}"
    )


def _parse_bonus(raw: Any, idx: int) -> tuple[BonusRule, ...]:
    if raw is None:
        return ()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise RateScheduleError(f"Period {idx}: 'feed_in_bonus' must be a list of rules")
    rules: list[BonusRule] = []
    names: set[str] = set()
    for n, item in enumerate(raw, start=1):
        where = f"Period {idx}, bonus {n}"
        if not isinstance(item, dict):
            raise RateScheduleError(f"{where} must be a mapping of field: value")
        unknown = set(item) - set(BONUS_FIELDS)
        if unknown:
            raise RateScheduleError(
                f"{where}: unknown field(s) {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(BONUS_FIELDS)}"
            )
        if "percent" not in item:
            raise RateScheduleError(f"{where} is missing 'percent'")
        try:
            percent = _to_number(item["percent"])
        except (TypeError, ValueError) as err:
            raise RateScheduleError(f"{where}: 'percent' must be a number") from err
        if not 0 < percent <= 200:
            raise RateScheduleError(f"{where}: 'percent' must be between 0 and 200 (e.g. 10 for 10%)")
        basis = str(item.get("of", BONUS_BASIS_MARKET)).strip().lower()
        if basis not in BONUS_BASES:
            raise RateScheduleError(f"{where}: 'of' must be one of {', '.join(BONUS_BASES)}")
        start = _parse_clock(item.get("from", "00:00"), where)
        end = _parse_clock(item.get("until", "24:00"), where)
        cap = item.get("max_kwh_per_year")
        if cap is not None:
            try:
                cap = _to_number(cap)
            except (TypeError, ValueError) as err:
                raise RateScheduleError(f"{where}: 'max_kwh_per_year' must be a number") from err
            if cap <= 0:
                raise RateScheduleError(f"{where}: 'max_kwh_per_year' must be above 0")
        only_positive = item.get("only_positive", True)
        if not isinstance(only_positive, bool):
            raise RateScheduleError(f"{where}: 'only_positive' must be true or false")
        name = item.get("name")
        name = str(name).strip() if name not in (None, "") else None
        rule = BonusRule(percent, basis, start, end, cap, only_positive, name)
        if rule.key in names:
            raise RateScheduleError(f"{where}: two bonus rules with the same name/settings")
        names.add(rule.key)
        rules.append(rule)
    return tuple(rules)


def parse_schedule(raw: Any) -> list[Rates]:
    """Validate a raw schedule and return fully resolved periods (sorted)."""
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        raise RateScheduleError("The schedule must be a list with at least one period")

    parsed: list[tuple[date, dict[str, Any]]] = []
    allowed = {"from", *FLOAT_FIELDS, *BOOL_FIELDS, *LIST_FIELDS}
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
        if "feed_in_bonus" in item:
            values["feed_in_bonus"] = _parse_bonus(item["feed_in_bonus"], idx)
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
    """Validate and return the user's schedule with ISO dates and clean times (for storage)."""
    parse_schedule(raw)  # raises on error
    items = [raw] if isinstance(raw, dict) else raw
    out = []
    for idx, item in enumerate(items, start=1):
        clean = dict(item)
        clean["from"] = _parse_date(item["from"], idx).isoformat()
        if "feed_in_bonus" in item:
            clean["feed_in_bonus"] = [
                {k: v for k, v in rule.as_dict().items() if v is not None}
                for rule in _parse_bonus(item["feed_in_bonus"], idx)
            ]
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


def _minutes(clock: str, sunrise: datetime | None, sunset: datetime | None) -> int | None:
    if clock == "sunrise":
        return None if sunrise is None else sunrise.hour * 60 + sunrise.minute
    if clock == "sunset":
        return None if sunset is None else sunset.hour * 60 + sunset.minute
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def in_window(rule: BonusRule, now: datetime, sunrise: datetime | None, sunset: datetime | None) -> bool:
    """Is ``now`` (local) inside the rule's daily window? Windows may wrap midnight."""
    start = _minutes(rule.start, sunrise, sunset)
    end = _minutes(rule.end, sunrise, sunset)
    if start is None or end is None:
        return False  # sun times unknown: be conservative
    current = now.hour * 60 + now.minute
    if start == end:
        return True if (start, end) == (0, 0) else False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def bonus_base(spot_excl_vat: float, rates: Rates, rule: BonusRule) -> float:
    """The amount (EUR/kWh excl. VAT) the bonus percentage is taken of."""
    if rule.of == BONUS_BASIS_MARKET_PLUS_CREDIT:
        return spot_excl_vat + rates.feed_in_credit
    return spot_excl_vat


def import_price(spot_excl_vat: float, rates: Rates) -> float:
    """All-in price per kWh bought: (spot + purchase fee + energy tax) + VAT."""
    return (spot_excl_vat + rates.purchase_fee + rates.energy_tax) * (1 + rates.vat)


def export_price(
    spot_excl_vat: float,
    rates: Rates,
    netting_active: bool,
    export_keeps_vat: bool,
    bonus_excl_vat: float = 0.0,
) -> float:
    """Value of one kWh fed into the grid.

    - Income from the supplier: spot price + fixed feed-in credit + bonus.
      You keep the VAT on it only when you are not VAT-registered
      (``export_keeps_vat``).
    - Feed-in fee (terugleverkosten) is charged on your bill incl. VAT.
    - While net metering applies, each netted kWh also avoids the energy tax
      (and optionally the purchase fee) incl. VAT on a kWh you bought.
    """
    income = spot_excl_vat + rates.feed_in_credit + bonus_excl_vat
    if export_keeps_vat:
        income *= 1 + rates.vat
    value = income - rates.feed_in_fee * (1 + rates.vat)
    if netting_active:
        netted = rates.energy_tax
        if rates.net_metering_includes_purchase_fee:
            netted += rates.purchase_fee
        value += netted * (1 + rates.vat)
    return value
