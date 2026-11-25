"""Price lists (today + tomorrow) for exact current prices and Predbat forecasts.

Sources, in order:
1. A sensor chosen in the options whose attributes hold a list of slots
   (``raw_today``/``raw_tomorrow``, ``prices``, ``forecast`` or ``data``),
   each with a start, optional end and a ``value``/``price``.
2. Otherwise the EnergyZero integration (also used by ANWB Energie), via its
   ``energyzero.get_energy_prices`` action, requested excluding VAT.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

ENERGYZERO_DOMAIN = "energyzero"
LIST_ATTRIBUTES = ("raw_today", "raw_tomorrow", "prices", "forecast", "data")
START_KEYS = ("start", "timestamp", "from", "startsAt", "start_time")
END_KEYS = ("end", "till", "to", "endsAt", "end_time")
VALUE_KEYS = ("value", "price", "total", "price_eur_kwh")


@dataclass(frozen=True)
class PriceSlot:
    """Spot price (EUR/kWh excl. VAT) for [start, end) in UTC."""

    start: datetime
    end: datetime
    spot_excl_vat: float


def _first(item: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in item and item[key] is not None:
            return item[key]
    return None


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = dt_util.parse_datetime(value)
    else:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_utc(dt)


def parse_price_items(
    items: Iterable[Any],
    includes_vat: bool,
    vat_for: Callable[[date], float],
    per_mwh: bool = False,
) -> list[PriceSlot]:
    """Turn a list of price dicts into sorted, de-duplicated slots."""
    raw: list[tuple[datetime, datetime | None, float]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        start = _as_utc(_first(item, START_KEYS))
        value = _first(item, VALUE_KEYS)
        if start is None or value is None:
            continue
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if per_mwh:
            price /= 1000
        if includes_vat:
            price /= 1 + vat_for(dt_util.as_local(start).date())
        raw.append((start, _as_utc(_first(item, END_KEYS)), price))

    raw.sort(key=lambda r: r[0])
    slots: list[PriceSlot] = []
    seen: set[datetime] = set()
    for idx, (start, end, price) in enumerate(raw):
        if start in seen:
            continue
        seen.add(start)
        if end is None or end <= start:
            if idx + 1 < len(raw) and raw[idx + 1][0] > start:
                end = raw[idx + 1][0]
            elif slots:
                end = start + (slots[-1].end - slots[-1].start)
            else:
                end = start + timedelta(hours=1)
        slots.append(PriceSlot(start, end, price))
    return slots


def read_entity_prices(
    hass: HomeAssistant, entity_id: str, includes_vat: bool, vat_for: Callable[[date], float]
) -> list[PriceSlot] | None:
    """Read a price list from a sensor's attributes, or None if there is none."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    items: list[Any] = []
    for attr in LIST_ATTRIBUTES:
        value = state.attributes.get(attr)
        if isinstance(value, list):
            items.extend(value)
    if not items:
        return None
    unit = str(state.attributes.get("unit_of_measurement", ""))
    return parse_price_items(items, includes_vat, vat_for, per_mwh="MWh" in unit)


def energyzero_entry_id(hass: HomeAssistant) -> str | None:
    """The first EnergyZero (or ANWB Energie) config entry, if installed."""
    entries = hass.config_entries.async_entries(ENERGYZERO_DOMAIN)
    return entries[0].entry_id if entries else None


async def _energyzero_call(
    hass: HomeAssistant, entry_id: str, start: datetime, end: datetime
) -> list[Any]:
    response = await hass.services.async_call(
        ENERGYZERO_DOMAIN,
        "get_energy_prices",
        {
            "config_entry": entry_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "incl_vat": False,
        },
        blocking=True,
        return_response=True,
    )
    prices = (response or {}).get("prices") if isinstance(response, dict) else None
    return prices if isinstance(prices, list) else []


async def fetch_energyzero_prices(
    hass: HomeAssistant, vat_for: Callable[[date], float]
) -> list[PriceSlot] | None:
    """Today's and (when published) tomorrow's spot prices excl. VAT."""
    entry_id = energyzero_entry_id(hass)
    if entry_id is None or not hass.services.has_service(ENERGYZERO_DOMAIN, "get_energy_prices"):
        return None
    today = dt_util.start_of_local_day()
    items: list[Any] = []
    try:
        items += await _energyzero_call(hass, entry_id, today, today + timedelta(days=1, seconds=-1))
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.debug("EnergyZero prices for today unavailable: %s", err)
        return None
    try:  # tomorrow is published around 14:00; failing before that is normal
        items += await _energyzero_call(
            hass, entry_id, today + timedelta(days=1), today + timedelta(days=2, seconds=-1)
        )
    except (HomeAssistantError, ValueError) as err:
        _LOGGER.debug("EnergyZero prices for tomorrow not yet available: %s", err)
    return parse_price_items(items, includes_vat=False, vat_for=vat_for) or None


def slot_at(slots: list[PriceSlot], when: datetime) -> PriceSlot | None:
    """The slot containing ``when`` (UTC or aware), if any."""
    when = dt_util.as_utc(when)
    for slot in slots:
        if slot.start <= when < slot.end:
            return slot
    return None
