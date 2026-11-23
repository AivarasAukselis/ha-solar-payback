"""Integration tests in a real Home Assistant test instance."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.solar_payback.const import DOMAIN

SOURCES = {
    "battery_discharge_power": "sensor.bat_dis",
    "grid_import_power": "sensor.grid_in",
    "grid_export_power": "sensor.grid_out",
    "house_load_power": "sensor.load",
    "spot_price": "sensor.spot",
    "spot_price_includes_vat": True,
    "export_keeps_vat": False,
    "battery_in_use_year": 2026,
}
SCHEDULE = [
    {"from": "2026-01-01", "energy_tax": 0.09161, "net_metering": True},
    {"from": "2027-01-01", "energy_tax": 0.08802, "net_metering": False},
]


def set_sources(hass, dis=0, imp=0, exp=0, load=0, spot_incl_vat=0.121):
    hass.states.async_set("sensor.bat_dis", str(dis), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_in", str(imp), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.grid_out", str(exp / 1000), {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.load", str(load), {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.spot", str(spot_incl_vat), {"unit_of_measurement": "€/kWh"})


async def setup_entry(hass) -> config_entries.ConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES)
    assert result["step_id"] == "rates"
    # invalid schedule shows an error and stays on the form
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rate_schedule": [{"from": "2026-01-01", "vat": 21}]}
    )
    assert bad["type"] is FlowResultType.FORM
    assert bad["errors"] == {"base": "invalid_schedule"}
    assert "fraction" in bad["description_placeholders"]["error"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"rate_schedule": SCHEDULE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def tick(hass, freezer, seconds):
    """Advance time in 10 s steps so every sample is integrated."""
    for _ in range(int(seconds // 10)):
        freezer.tick(timedelta(seconds=10))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()


@pytest.fixture
def frozen(freezer):
    freezer.move_to("2026-11-23 18:00:00+01:00")
    return freezer


async def test_full_flow_and_calculation(hass: HomeAssistant, frozen) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam")
    set_sources(hass)
    entry = await setup_entry(hass)

    # One device with all entities
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1 and devices[0].name == "Solar & Battery"
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 23  # 21 sensors + 2 numbers
    assert all(e.device_id == devices[0].id for e in entities)

    # Evening: battery 2 kW powers the house, spot 0.10 excl VAT (0.121 incl)
    set_sources(hass, dis=2000, load=2000, spot_incl_vat=0.121)
    await tick(hass, frozen, 3600)

    expected = 2 * (0.10 + 0.09161) * 1.21  # 2 kWh x all-in import price
    savings = float(hass.states.get("sensor.solar_battery_savings_today").state)
    assert savings == pytest.approx(expected, abs=0.02)
    assert hass.states.get("sensor.solar_battery_battery_private_use_this_year").state == "100.0"
    assert float(hass.states.get("sensor.solar_battery_import_price_all_in").state) == pytest.approx(
        (0.10 + 0.09161) * 1.21, abs=1e-4
    )
    mode = hass.states.get("sensor.solar_battery_pricing_mode")
    assert mode.attributes["next_rate_change"]["from"] == "2027-01-01"

    # Battery exports 1 kW to grid for an hour: private use drops to 2/3
    set_sources(hass, dis=1000, exp=1000, spot_incl_vat=0.121)
    await tick(hass, frozen, 3600)
    pct = float(hass.states.get("sensor.solar_battery_battery_private_use_this_year").state)
    assert pct == pytest.approx(66.67, abs=0.5)

    # Settings via number entities -> VAT on private use and payback
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.solar_battery_battery_cost_excl_vat", "value": 10000}, blocking=True,
    )
    await hass.services.async_call(
        "number", "set_value",
        {"entity_id": "number.solar_battery_investment_after_vat_refund", "value": 1000}, blocking=True,
    )
    vat = float(hass.states.get("sensor.solar_battery_vat_on_private_use_this_year").state)
    assert vat == pytest.approx(10000 / 5 * pct / 100 * 0.21, abs=3)  # pct state may be up to 60 s old
    assert hass.states.get("sensor.solar_battery_payback_progress").state not in ("unknown", "unavailable")

    # Service to set a counter
    await hass.services.async_call(
        DOMAIN, "set_counter", {"counter": "savings_total", "value": 500}, blocking=True
    )
    assert hass.states.get("sensor.solar_battery_savings_total").state == "500.0"
    assert float(hass.states.get("sensor.solar_battery_payback_remaining").state) == 500.0

    # Counters survive a reload
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.solar_battery_savings_total").state == "500.0"


async def test_unavailable_source_is_skipped(hass: HomeAssistant, frozen) -> None:
    set_sources(hass, dis=2000, load=2000)
    entry = await setup_entry(hass)
    hass.states.async_set("sensor.load", "unavailable")
    await hass.async_block_till_done()
    await tick(hass, frozen, 600)
    assert float(hass.states.get("sensor.solar_battery_battery_to_house_this_year").state) == 0.0
    mode = hass.states.get("sensor.solar_battery_pricing_mode")
    assert "house_load_power" in mode.attributes["input_problem"]
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_options_flow_updates_schedule(hass: HomeAssistant, frozen) -> None:
    set_sources(hass)
    entry = await setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], SOURCES)
    assert result["step_id"] == "rates"
    new = SCHEDULE + [{"from": "2027-07-01", "purchase_fee": 0.03}]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"rate_schedule": new}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(entry.options["rate_schedule"]) == 3
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_single_instance(hass: HomeAssistant, frozen) -> None:
    set_sources(hass)
    await setup_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT


async def test_switches_to_2027_rules_at_midnight(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam")
    freezer.move_to("2026-12-31 23:50:00+01:00")
    set_sources(hass, imp=3000, load=3000)
    await setup_entry(hass)
    await tick(hass, freezer, 300)  # import some energy so netting applies
    set_sources(hass, exp=2000)
    await tick(hass, freezer, 120)
    assert hass.states.get("sensor.solar_battery_pricing_mode").state == "net_metering"
    await tick(hass, freezer, 600)  # crosses midnight
    assert hass.states.get("sensor.solar_battery_pricing_mode").state == "no_net_metering"
    assert float(hass.states.get("sensor.solar_battery_feed_in_value").state) == pytest.approx(0.10, abs=1e-4)
    prev = hass.states.get("sensor.solar_battery_battery_private_use_this_year").attributes["previous_year"]
    assert prev["year"] == 2026 and prev["grid_import_kwh"] > 0


async def test_short_spike_between_heartbeats_is_counted(hass: HomeAssistant, frozen) -> None:
    set_sources(hass)
    entry = await setup_entry(hass)
    engine = entry.runtime_data.engine
    await tick(hass, frozen, 10)
    # 3 s spike of 3.6 kW battery to house, entirely between two heartbeats
    frozen.tick(timedelta(seconds=2))
    set_sources(hass, dis=3600, load=3600)
    await hass.async_block_till_done()
    frozen.tick(timedelta(seconds=3))
    set_sources(hass)
    await hass.async_block_till_done()
    await tick(hass, frozen, 20)
    # 3.6 kW x 3 s = 3 Wh
    assert engine.counters["battery_to_home_year"] == pytest.approx(0.003, abs=1e-6)


P1 = {
    "grid_import_energy": ["sensor.p1_in_t1", "sensor.p1_in_t2"],
    "grid_export_energy": ["sensor.p1_out_t1", "sensor.p1_out_t2"],
}


def set_p1(hass, in1, in2, out1, out2):
    for eid, val in (("sensor.p1_in_t1", in1), ("sensor.p1_in_t2", in2),
                     ("sensor.p1_out_t1", out1), ("sensor.p1_out_t2", out2)):
        hass.states.async_set(eid, str(val), {"unit_of_measurement": "kWh", "device_class": "energy"})


async def test_p1_counters_and_fallback(hass: HomeAssistant, frozen) -> None:
    set_sources(hass)
    set_p1(hass, 1000.0, 2000.0, 100.0, 200.0)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    # only import counters -> error
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SOURCES, "grid_import_energy": P1["grid_import_energy"]}
    )
    assert bad["errors"] == {"base": "counters_pair"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**SOURCES, **P1})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rate_schedule": SCHEDULE})
    entry = result["result"]
    await hass.async_block_till_done()
    engine = entry.runtime_data.engine

    # Power sensors claim 3 kW import, but the meter counts 0.5 kWh (t1) + 0.25 kWh (t2)
    set_sources(hass, imp=3000, load=3000)
    await tick(hass, frozen, 600)
    set_p1(hass, 1000.5, 2000.25, 100.0, 200.0)
    await tick(hass, frozen, 10)
    assert engine.counters["grid_import_year"] == pytest.approx(0.75, abs=1e-6)
    mode = hass.states.get("sensor.solar_battery_pricing_mode")
    assert mode.attributes["grid_energy_source"] == "p1_counters"

    # P1 drops out: falls back to power (3 kW x 60 s = 0.05 kWh)
    hass.states.async_set("sensor.p1_in_t1", "unavailable")
    await hass.async_block_till_done()
    await tick(hass, frozen, 60)
    assert engine.counters["grid_import_year"] == pytest.approx(0.75 + 0.05, abs=0.006)
    await tick(hass, frozen, 60)  # let entities refresh
    mode = hass.states.get("sensor.solar_battery_pricing_mode")
    assert mode.attributes["grid_energy_source"] == "power_fallback"

    # P1 back with a big jump (it counted while unavailable): re-baselined, not double counted
    set_p1(hass, 1001.5, 2000.25, 100.0, 200.0)
    before = engine.counters["grid_import_year"]
    await tick(hass, frozen, 10)
    set_p1(hass, 1001.6, 2000.25, 100.0, 200.0)
    await tick(hass, frozen, 10)
    assert engine.counters["grid_import_year"] - before == pytest.approx(0.1 + 3 * 10 / 3600, abs=0.01)


async def test_feed_in_bonus_in_home_assistant(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_update(time_zone="Europe/Amsterdam", latitude=52.37, longitude=4.89)
    freezer.move_to("2027-06-01 12:00:00+02:00")
    set_sources(hass, exp=1000, spot_incl_vat=0.121)
    schedule = [{"from": "2027-01-01", "energy_tax": 0.08802, "net_metering": False,
                 "feed_in_credit": 0.02,
                 "feed_in_bonus": [{"name": "allday", "percent": 50, "from": "00:00", "until": "24:00"},
                                   {"name": "daylight", "percent": 10, "of": "market_plus_credit",
                                    "from": "sunrise", "until": "sunset"}]}]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SOURCES)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rate_schedule": schedule})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    await tick(hass, freezer, 3600)
    # VAT-registered: per kWh 0.10 + 0.02 + 50% x 0.10 + 10% x 0.12 = 0.182
    value = hass.states.get("sensor.solar_battery_feed_in_value")
    assert float(value.state) == pytest.approx(0.182, abs=1e-4)
    assert value.attributes["bonus_rules_active"] == ["allday", "daylight"]
    bonus = float(hass.states.get("sensor.solar_battery_feed_in_bonus_this_year").state)
    assert bonus == pytest.approx(0.062, abs=0.005)  # 1 kWh x (0.05 + 0.012), state rounded to cents
