"""Unit tests for the rate schedule and calculation engine."""

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.solar_payback.const import (
    MODE_NET_METERING,
    MODE_NET_METERING_SURPLUS,
    MODE_NO_NET_METERING,
)
from custom_components.solar_payback.engine import Engine, Inputs
from custom_components.solar_payback.rates import (
    DEFAULT_SCHEDULE,
    RateSchedule,
    RateScheduleError,
    export_price,
    import_price,
    normalize_schedule,
    parse_schedule,
)

TZ = timezone(timedelta(hours=1))


def t(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=TZ)


# ------------------------------------------------------------------ schedule
def test_default_schedule_parses_and_inherits():
    sched = RateSchedule(parse_schedule(DEFAULT_SCHEDULE))
    r26 = sched.at(date(2026, 11, 23))
    r27 = sched.at(date(2027, 3, 1))
    assert r26.net_metering is True
    assert r26.energy_tax == pytest.approx(0.09161)
    assert r27.net_metering is False
    assert r27.energy_tax == pytest.approx(0.08802)
    assert r27.vat == 0.21  # inherited
    assert sched.next_change(date(2026, 12, 31)).start == date(2027, 1, 1)
    assert sched.next_change(date(2027, 1, 1)) is None
    # before the first period -> first period
    assert sched.at(date(2020, 1, 1)).start == date(2026, 1, 1)


def test_unsorted_and_timestamp_dates():
    raw = [
        {"from": "2027-07-01T00:00:00.000Z", "purchase_fee": 0.02},
        {"from": "2026-01-01", "energy_tax": 0.1, "net_metering": True},
    ]
    periods = parse_schedule(raw)
    assert [p.start for p in periods] == [date(2026, 1, 1), date(2027, 7, 1)]
    assert periods[1].energy_tax == 0.1 and periods[1].net_metering is True
    assert normalize_schedule(raw)[0]["from"] == "2026-01-01"


@pytest.mark.parametrize(
    ("raw", "msg"),
    [
        ([], "at least one"),
        ([{"energy_tax": 0.1}], "missing 'from'"),
        ([{"from": "nope"}], "must be a date"),
        ([{"from": "2026-01-01", "vat": 21}], "fraction"),
        ([{"from": "2026-01-01", "tax": 0.1}], "unknown field"),
        ([{"from": "2026-01-01", "net_metering": "yes"}], "true or false"),
        ([{"from": "2026-01-01"}, {"from": "2026-01-01"}], "same date"),
        ([{"from": "2026-01-01", "energy_tax": 110}], "too large"),
    ],
)
def test_schedule_errors(raw, msg):
    with pytest.raises(RateScheduleError, match=msg):
        parse_schedule(raw)


def test_price_model():
    r = parse_schedule(
        [{"from": "2026-01-01", "energy_tax": 0.09161, "purchase_fee": 0.02,
          "feed_in_fee": 0.01, "net_metering": True}]
    )[0]
    spot = 0.10
    assert import_price(spot, r) == pytest.approx((0.10 + 0.02 + 0.09161) * 1.21)
    # VAT-registered, netting: spot excl VAT + (tax - fee) incl VAT
    assert export_price(spot, r, True, False) == pytest.approx(0.10 + (0.09161 - 0.01) * 1.21)
    # private person, no netting
    assert export_price(spot, r, False, True) == pytest.approx((0.10 - 0.01) * 1.21)


# -------------------------------------------------------------------- engine
def make_engine():
    e = Engine()
    e.battery_in_use_year = 2026
    e.export_keeps_vat = False
    return e


def test_battery_split_and_private_use():
    e = make_engine()
    # 1 h: battery 3 kW, house 1 kW, export 2 kW -> 2 kWh to grid, 1 kWh to house
    e.step(t(2026, 11, 23), 1.0, Inputs(3000, 0, 2000, 1000, 0.10))
    # 1 h: battery 1 kW to house, no export
    e.step(t(2026, 11, 23, 20), 1.0, Inputs(1000, 0, 0, 1000, 0.10))
    c = e.counters
    assert c["battery_to_grid_year"] == pytest.approx(2.0)
    assert c["battery_to_home_year"] == pytest.approx(2.0)
    assert e.private_use_pct() == pytest.approx(50.0)
    e.settings["battery_cost_excl_vat"] = 10000
    # Belastingdienst formula: 10000 / 5 x 50% x 21% = 210
    assert e.vat_on_private_use(2026) == pytest.approx(210.0)


def test_savings_and_net_metering_switch_2027():
    e = make_engine()
    # Import 10 kWh first so export stays below import (netting applies)
    e.step(t(2026, 12, 30, 2), 1.0, Inputs(0, 10000, 0, 10000, 0.10))
    assert e.counters["savings_year"] == pytest.approx(0.0)
    # Export 1 kWh of solar while house uses 0: value = spot + tax*1.21
    e.step(t(2026, 12, 30, 13), 1.0, Inputs(0, 0, 1000, 0, 0.10))
    assert e.last.mode == MODE_NET_METERING
    assert e.counters["savings_year"] == pytest.approx(0.10 + 0.09161 * 1.21)

    # New year: counters reset, snapshot kept, no more netting
    e.step(t(2027, 1, 1, 13), 1.0, Inputs(0, 0, 1000, 0, 0.10))
    assert e.previous_year["year"] == 2026
    assert e.previous_year["savings"] == pytest.approx(round(0.10 + 0.09161 * 1.21, 2))
    assert e.last.mode == MODE_NO_NET_METERING
    assert e.counters["savings_year"] == pytest.approx(0.10)
    assert e.counters["grid_import_year"] == 0.0


def test_net_metering_surplus():
    e = make_engine()
    e.step(t(2026, 6, 1), 1.0, Inputs(0, 1000, 0, 1000, 0.10))  # import 1 kWh
    e.step(t(2026, 6, 1, 13), 1.0, Inputs(0, 0, 1000, 0, 0.10))  # export 1 kWh -> netted
    assert e.last.mode == MODE_NET_METERING
    e.step(t(2026, 6, 1, 14), 1.0, Inputs(0, 0, 1000, 0, 0.10))  # export > import now
    assert e.last.mode == MODE_NET_METERING_SURPLUS
    assert e.last.export_price == pytest.approx(0.10)


def test_self_consumption_saving():
    e = make_engine()
    # House 2 kW fully covered by battery: saves 2 kWh x import price
    e.step(t(2026, 11, 23, 19), 1.0, Inputs(2000, 0, 0, 2000, 0.20))
    expected = 2 * (0.20 + 0.09161) * 1.21
    assert e.counters["savings_day"] == pytest.approx(expected)
    assert e.counters["cost_actual_year"] == pytest.approx(0.0)


def test_rollover_day_and_month():
    e = make_engine()
    e.step(t(2026, 11, 30, 19), 1.0, Inputs(1000, 0, 0, 1000, 0.20))
    assert e.counters["savings_day"] > 0
    e.step(t(2026, 12, 1, 1), 0.0, Inputs(0, 0, 0, 0, 0.20))
    assert e.counters["savings_day"] == 0
    assert e.counters["savings_month"] == 0
    assert e.counters["savings_year"] > 0


def test_vat_outside_adjustment_period():
    e = make_engine()
    e.battery_in_use_year = 2020
    e.settings["battery_cost_excl_vat"] = 10000
    e.step(t(2026, 11, 23), 1.0, Inputs(1000, 0, 0, 1000, 0.10))
    assert e.adjustment_year(2026) is None
    assert e.vat_on_private_use(2026) == 0.0


def test_payback():
    e = make_engine()
    e.settings["investment"] = 1000
    start = t(2026, 1, 1)
    e.step(start, 0.0, Inputs(0, 0, 0, 0, 0.1))
    e.counters["savings_total"] = e.counters["savings_tracked"] = 100.0
    now = start + timedelta(days=10)  # 10 EUR/day
    assert e.payback_progress() == pytest.approx(10.0)
    assert e.payback_remaining() == pytest.approx(900.0)
    assert e.payback_estimate(now) == (now + timedelta(days=90)).date()
    # too little history -> no estimate
    assert e.payback_estimate(start + timedelta(days=3)) is None


def test_no_price_still_counts_energy():
    e = make_engine()
    e.step(t(2026, 11, 23), 1.0, Inputs(1000, 0, 0, 1000, None))
    assert e.counters["battery_to_home_year"] == pytest.approx(1.0)
    assert e.counters["savings_total"] == 0.0


def test_roundtrip_storage():
    e = make_engine()
    e.settings["investment"] = 5000
    e.step(t(2026, 11, 23), 1.0, Inputs(1000, 500, 200, 1300, 0.1))
    e2 = Engine(e.to_dict())
    assert e2.counters == e.counters
    assert e2.settings == e.settings
    assert e2.year == 2026 and e2.day == "2026-11-23"


def test_counter_energy_replaces_power_for_grid():
    e = make_engine()
    # power says 5 kW import for 1 h, but the meter counted 4.2 kWh: meter wins
    e.step(t(2026, 11, 23), 1.0, Inputs(0, 5000, 0, 4200, 0.10), grid_import_kwh=4.2, grid_export_kwh=0.0)
    assert e.counters["grid_import_year"] == pytest.approx(4.2)
    assert e.counters["cost_actual_year"] == pytest.approx(4.2 * (0.10 + 0.09161) * 1.21)
    # counters with dt == 0 are still counted
    e.step(t(2026, 11, 23, 13), 0.0, Inputs(0, 0, 0, 0, 0.10), grid_import_kwh=0.1, grid_export_kwh=0.0)
    assert e.counters["grid_import_year"] == pytest.approx(4.3)


# --------------------------------------------------------------- bonuses
from custom_components.solar_payback.rates import BonusRule, in_window  # noqa: E402

ZONNEPLAN = [
    {"from": "2027-01-01", "energy_tax": 0.08802, "purchase_fee": 0.016529,
     "feed_in_credit": 0.016529, "net_metering": False,
     "feed_in_bonus": [{"name": "zonnebonus", "percent": 10, "of": "market_plus_credit",
                        "from": "sunrise", "until": "sunset", "max_kwh_per_year": 2}]},
]
SUNRISE = datetime(2027, 6, 1, 5, 20, tzinfo=TZ)
SUNSET = datetime(2027, 6, 1, 22, 0, tzinfo=TZ)


def bonus_engine(schedule, keep_vat=False):
    e = make_engine()
    e.schedule = RateSchedule(parse_schedule(schedule))
    e.export_keeps_vat = keep_vat
    return e


def test_bonus_parsing_and_errors():
    r = parse_schedule(ZONNEPLAN)[0]
    rule = r.feed_in_bonus[0]
    assert (rule.percent, rule.of, rule.start, rule.end, rule.max_kwh_per_year) == (
        10, "market_plus_credit", "sunrise", "sunset", 2)
    # YAML 1.1 turns unquoted 06:00 into 360 -> accepted
    r2 = parse_schedule([{"from": "2027-01-01", "feed_in_bonus": [{"percent": 50, "from": 360, "until": "22:00"}]}])[0]
    assert r2.feed_in_bonus[0].start == "06:00"
    for bad, msg in [
        ({"percent": 0}, "between 0 and 200"),
        ({"percent": 10, "of": "spot"}, "'of' must be"),
        ({"percent": 10, "from": "25:00"}, "time must be"),
        ({"percent": 10, "max_kwh_per_year": -1}, "above 0"),
        ({"pct": 10}, "unknown field"),
    ]:
        with pytest.raises(RateScheduleError, match=msg):
            parse_schedule([{"from": "2027-01-01", "feed_in_bonus": [bad]}])
    # inherited by later periods, removable with []
    p = parse_schedule(ZONNEPLAN + [{"from": "2027-06-01", "purchase_fee": 0.02},
                                    {"from": "2028-01-01", "feed_in_bonus": []}])
    assert p[1].feed_in_bonus == p[0].feed_in_bonus
    assert p[2].feed_in_bonus == ()


def test_in_window_variants():
    day = BonusRule(10, start="sunrise", end="sunset")
    assert in_window(day, datetime(2027, 6, 1, 12, 0, tzinfo=TZ), SUNRISE, SUNSET)
    assert not in_window(day, datetime(2027, 6, 1, 23, 0, tzinfo=TZ), SUNRISE, SUNSET)
    assert not in_window(day, datetime(2027, 6, 1, 12, 0, tzinfo=TZ), None, None)  # unknown sun
    evening = BonusRule(50, start="06:00", end="22:00")
    assert in_window(evening, datetime(2027, 6, 1, 21, 59, tzinfo=TZ), None, None)
    assert not in_window(evening, datetime(2027, 6, 1, 22, 0, tzinfo=TZ), None, None)
    night = BonusRule(5, start="22:00", end="06:00")  # wraps midnight
    assert in_window(night, datetime(2027, 6, 1, 23, 30, tzinfo=TZ), None, None)
    assert in_window(night, datetime(2027, 6, 1, 3, 0, tzinfo=TZ), None, None)
    assert not in_window(night, datetime(2027, 6, 1, 12, 0, tzinfo=TZ), None, None)
    assert in_window(BonusRule(5), datetime(2027, 6, 1, 0, 0, tzinfo=TZ), None, None)  # all day


def test_zonneplan_bonus_value_cap_and_window():
    e = bonus_engine(ZONNEPLAN, keep_vat=True)
    noon = datetime(2027, 6, 1, 13, 0, tzinfo=TZ)
    # feed in 1 kWh at 10 ct spot: (0.10 + 0.016529) * 1.10 * 1.21 per kWh (private person)
    e.step(noon, 1.0, Inputs(0, 0, 1000, 0, 0.10, SUNRISE, SUNSET))
    expected = (0.10 + 0.016529) * 1.10 * 1.21
    assert e.counters["savings_year"] == pytest.approx(expected)
    assert e.counters["feed_in_bonus_year"] == pytest.approx(0.10 * 0.116529 * 1.21)
    assert e.last.bonus_rules_active == ("zonnebonus",)
    # 3 kWh more, only 1 kWh left under the 2 kWh cap -> bonus on 1 of 3
    before = e.counters["feed_in_bonus_year"]
    e.step(noon, 1.0, Inputs(0, 0, 3000, 0, 0.10, SUNRISE, SUNSET))
    assert e.counters["feed_in_bonus_year"] - before == pytest.approx(0.10 * 0.116529 * 1.21)
    assert e.bonus_kwh_year["zonnebonus"] == pytest.approx(2.0)
    # cap reached: no bonus shown any more
    e.step(noon, 0.1, Inputs(0, 0, 1000, 0, 0.10, SUNRISE, SUNSET))
    assert e.last.bonus_rules_active == ()
    # new year resets the cap and keeps last year's bonus
    e.step(datetime(2028, 6, 1, 13, 0, tzinfo=TZ), 1.0, Inputs(0, 0, 1000, 0, 0.10, SUNRISE, SUNSET))
    assert e.previous_year["feed_in_bonus"] > 0
    assert e.bonus_kwh_year["zonnebonus"] == pytest.approx(1.0)


def test_bonus_outside_window_and_negative_price():
    e = bonus_engine(ZONNEPLAN)
    night = datetime(2027, 6, 1, 23, 0, tzinfo=TZ)
    e.step(night, 1.0, Inputs(0, 0, 1000, 0, 0.10, SUNRISE, SUNSET))
    assert e.counters["feed_in_bonus_year"] == 0
    # VAT-registered: spot + credit, no VAT, no bonus at night
    assert e.counters["savings_year"] == pytest.approx(0.10 + 0.016529)
    noon = datetime(2027, 6, 1, 13, 0, tzinfo=TZ)
    e.step(noon, 1.0, Inputs(0, 0, 1000, 0, -0.05, SUNRISE, SUNSET))  # base < 0
    assert e.counters["feed_in_bonus_year"] == 0


def test_nextenergy_style_market_bonus_evening():
    sched = [{"from": "2027-01-01", "energy_tax": 0.08802, "purchase_fee": 0.018099,
              "net_metering": False,
              "feed_in_bonus": [{"percent": 50, "from": "06:00", "until": "22:00", "max_kwh_per_year": 6000}]}]
    e = bonus_engine(sched)
    e.step(datetime(2027, 1, 15, 19, 0, tzinfo=TZ), 1.0, Inputs(2000, 0, 2000, 0, 0.20))
    assert e.counters["savings_year"] == pytest.approx(2 * 0.20 * 1.5)
    assert e.last.export_price == pytest.approx(0.30)
