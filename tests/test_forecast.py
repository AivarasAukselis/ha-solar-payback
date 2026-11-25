"""Price list parsing, slot pricing and the Predbat forecast sensors."""

from datetime import date, datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.solar_payback.const import DOMAIN
from custom_components.solar_payback.engine import Engine
from custom_components.solar_payback.prices import parse_price_items, slot_at
from custom_components.solar_payback.rates import RateSchedule, parse_schedule

from .test_integration import SOURCES, set_sources, tick

UTC = timezone.utc
TZ = timezone(timedelta(hours=1))
ANWB = [
    {"from": "2026-01-01", "energy_tax": 0.09161, "purchase_fee": 0.014876, "vat": 0.21,
     "net_metering": True, "net_metering_includes_purchase_fee": True},
    {"from": "2027-01-01", "energy_tax": 0.08802, "net_metering": False},
]


def vat(_day: date) -> float:
    return 0.21


# ------------------------------------------------------------------ parsing
def test_parse_start_end_and_timestamp_formats():
    a = parse_price_items(
        [{"start": "2026-11-25T10:00:00+01:00", "end": "2026-11-25T11:00:00+01:00", "price": 0.2},
         {"start": "2026-11-25T09:00:00+01:00", "end": "2026-11-25T10:00:00+01:00", "price": 0.1}],
        includes_vat=False, vat_for=vat)
    assert [s.spot_excl_vat for s in a] == [0.1, 0.2]  # sorted
    b = parse_price_items(
        [{"timestamp": "2026-11-25 09:00:00+00:00", "price": 0.121},
         {"timestamp": "2026-11-25 09:15:00+00:00", "price": 0.242}],
        includes_vat=True, vat_for=vat)
    assert b[0].end == datetime(2026, 11, 25, 9, 15, tzinfo=UTC)  # inferred from next
    assert b[1].end == datetime(2026, 11, 25, 9, 30, tzinfo=UTC)  # same length as previous
    assert b[0].spot_excl_vat == pytest.approx(0.1)
    c = parse_price_items([{"start": "2026-11-25T09:00:00+00:00", "value": 100}], False, vat, per_mwh=True)
    assert c[0].spot_excl_vat == pytest.approx(0.1)
    assert slot_at(b, datetime(2026, 11, 25, 9, 20, tzinfo=UTC)).spot_excl_vat == pytest.approx(0.2)
    assert slot_at(b, datetime(2026, 11, 25, 10, 0, tzinfo=UTC)) is None


# ------------------------------------------------------------- slot pricing
def anwb_engine(schedule=ANWB):
    e = Engine()
    e.schedule = RateSchedule(parse_schedule(schedule))
    e.export_keeps_vat = False
    e.rollover(datetime(2026, 12, 31, 12, tzinfo=TZ))
    return e


def test_slot_prices_net_metering_then_2027():
    e = anwb_engine()
    e.counters["grid_import_year"], e.counters["grid_export_year"] = 3000, 1000
    p_in, p_out = e.slot_prices(datetime(2026, 12, 31, 18, tzinfo=TZ), 0.20, None, None)
    assert p_in == pytest.approx((0.20 + 0.014876 + 0.09161) * 1.21)
    assert p_out == pytest.approx(0.20 + (0.09161 + 0.014876) * 1.21)  # VAT-registered: spot excl VAT
    p_in27, p_out27 = e.slot_prices(datetime(2027, 1, 1, 18, tzinfo=TZ), 0.20, None, None)
    assert p_in27 == pytest.approx((0.20 + 0.014876 + 0.08802) * 1.21)
    assert p_out27 == pytest.approx(0.20)
    # surplus: export above import this year -> no netting
    e.counters["grid_export_year"] = 4000
    assert e.slot_prices(datetime(2026, 12, 31, 18, tzinfo=TZ), 0.20, None, None)[1] == pytest.approx(0.20)


def test_slot_prices_bonus_window_and_cap():
    sched = [{"from": "2027-01-01", "energy_tax": 0.08802, "net_metering": False,
              "feed_in_bonus": [{"name": "b", "percent": 50, "from": "06:00", "until": "22:00",
                                 "max_kwh_per_year": 10}]}]
    e = anwb_engine(sched)
    e.rollover(datetime(2027, 1, 2, 12, tzinfo=TZ))
    assert e.slot_prices(datetime(2027, 1, 2, 19, tzinfo=TZ), 0.20, None, None)[1] == pytest.approx(0.30)
    assert e.slot_prices(datetime(2027, 1, 2, 23, tzinfo=TZ), 0.20, None, None)[1] == pytest.approx(0.20)
    e.bonus_kwh_year["b"] = 10  # cap used up
    assert e.slot_prices(datetime(2027, 1, 2, 19, tzinfo=TZ), 0.20, None, None)[1] == pytest.approx(0.20)


# --------------------------------------------------- Home Assistant, EnergyZero
def energyzero_service(hass, calls):
    """Fake energyzero.get_energy_prices with hourly prices (excl. VAT): 0.10 + hour/100."""

    async def handler(call: ServiceCall):
        calls.append(dict(call.data))
        start = dt_util.parse_datetime(call.data["start"])
        end = dt_util.parse_datetime(call.data["end"])
        if start.date() > dt_util.now().date() and dt_util.now().hour < 14:
            raise ValueError("tomorrow not published yet")
        out, t = [], dt_util.as_utc(start)
        while t < dt_util.as_utc(end):
            local = dt_util.as_local(t)
            out.append({"start": t.isoformat(), "end": (t + timedelta(hours=1)).isoformat(),
                        "price": round(0.10 + local.hour / 100, 4)})
            t += timedelta(hours=1)
        return {"prices": out}

    hass.services.async_register("energyzero", "get_energy_prices", handler,
                                 supports_response=SupportsResponse.ONLY)
    MockConfigEntry(domain="energyzero", entry_id="ez1").add_to_hass(hass)


async def setup_with(hass, schedule, extra=None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SOURCES, **(extra or {})})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rate_schedule": schedule})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def test_energyzero_forecast_over_new_year(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam")
    freezer.move_to("2026-12-31 15:00:00+01:00")
    calls: list = []
    energyzero_service(hass, calls)
    set_sources(hass, imp=2000, load=2000, spot_incl_vat=0.999)  # sensor price must NOT be used
    entry = await setup_with(hass, ANWB)
    entry.runtime_data.engine.counters.update(grid_import_year=3000, grid_export_year=1000)
    await tick(hass, freezer, 60)
    assert calls and calls[0]["incl_vat"] is False and calls[0]["config_entry"] == "ez1"

    imp = hass.states.get("sensor.solar_battery_import_price_forecast")
    exp = hass.states.get("sensor.solar_battery_export_price_forecast")
    assert imp.attributes["source"] == "energyzero"
    assert imp.attributes["slots"] == 48  # today + tomorrow (published after 14:00)
    by_start = {s["start"]: s["value"] for s in exp.attributes["raw_today"]}
    # 31 Dec 18:00: net metering -> feed-in worth spot + (tax + fee) incl VAT
    assert by_start["2026-12-31T18:00:00+01:00"] == pytest.approx(0.28 + (0.09161 + 0.014876) * 1.21, abs=1e-5)
    # 1 Jan 18:00: bare spot
    assert by_start["2027-01-01T18:00:00+01:00"] == pytest.approx(0.28, abs=1e-5)
    # state = current slot (15:00 -> spot 0.25), and the money uses the list, not the 0.999 sensor
    assert float(imp.state) == pytest.approx((0.25 + 0.014876 + 0.09161) * 1.21, abs=1e-4)
    assert float(hass.states.get("sensor.solar_battery_import_price_all_in").state) == pytest.approx(
        float(imp.state), abs=1e-4)

    # exact switch at the hour: 15:59:30 -> 16:00:01
    freezer.move_to("2026-12-31 15:59:30+01:00")
    async_fire_time_changed(hass); await hass.async_block_till_done()
    freezer.move_to("2026-12-31 16:00:01+01:00")
    async_fire_time_changed(hass); await hass.async_block_till_done()
    imp = hass.states.get("sensor.solar_battery_import_price_forecast")
    assert float(imp.state) == pytest.approx((0.26 + 0.014876 + 0.09161) * 1.21, abs=1e-4)
    assert float(hass.states.get("sensor.solar_battery_import_price_all_in").state) == pytest.approx(
        float(imp.state), abs=1e-4)


async def test_price_list_entity_source(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam")
    freezer.move_to("2027-03-10 12:00:00+01:00")
    today = dt_util.start_of_local_day()
    raw = [{"start": (today + timedelta(hours=h)).isoformat(),
            "end": (today + timedelta(hours=h + 1)).isoformat(),
            "value": round(0.121 * (1 + h / 10), 5)} for h in range(24)]
    hass.states.async_set("sensor.my_prices", "0.1", {"raw_today": raw, "unit_of_measurement": "€/kWh"})
    set_sources(hass)
    sched = [{"from": "2027-01-01", "energy_tax": 0.08802, "net_metering": False, "feed_in_credit": 0.02}]
    await setup_with(hass, sched, {"price_list_entity": "sensor.my_prices"})
    await tick(hass, freezer, 20)
    exp = hass.states.get("sensor.solar_battery_export_price_forecast")
    assert exp.attributes["source"] == "entity"
    noon = next(s for s in exp.attributes["raw_today"] if s["start"].startswith("2027-03-10T12:00"))
    # list includes VAT (default option on): spot = 0.121*2.2/1.21 = 0.22; + credit 0.02
    assert noon["value"] == pytest.approx(0.24, abs=1e-5)
