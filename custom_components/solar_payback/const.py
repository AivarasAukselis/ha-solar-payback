"""Constants for Solar & Battery Payback."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "solar_payback"

CONF_BATTERY_DISCHARGE = "battery_discharge_power"
CONF_GRID_IMPORT = "grid_import_power"
CONF_GRID_EXPORT = "grid_export_power"
CONF_HOUSE_LOAD = "house_load_power"
CONF_SPOT_PRICE = "spot_price"
CONF_SPOT_INCLUDES_VAT = "spot_price_includes_vat"
CONF_EXPORT_KEEPS_VAT = "export_keeps_vat"
CONF_BATTERY_IN_USE_YEAR = "battery_in_use_year"
CONF_RATE_SCHEDULE = "rate_schedule"
CONF_GRID_IMPORT_ENERGY = "grid_import_energy"
CONF_GRID_EXPORT_ENERGY = "grid_export_energy"

# A single counter jump above this is treated as a meter reset/glitch.
MAX_COUNTER_STEP_KWH = 50.0
# Entities are refreshed at most this often (keeps the recorder database small).
NOTIFY_INTERVAL = timedelta(seconds=60)

GRID_SOURCE_COUNTERS = "p1_counters"
GRID_SOURCE_POWER = "power"
GRID_SOURCE_POWER_FALLBACK = "power_fallback"

POWER_SOURCES = (
    CONF_BATTERY_DISCHARGE,
    CONF_GRID_IMPORT,
    CONF_GRID_EXPORT,
    CONF_HOUSE_LOAD,
)

# Energy is integrated on every change of a source sensor; this heartbeat
# also integrates while readings stay constant.
TICK_INTERVAL = timedelta(seconds=10)
# A gap longer than this (e.g. HA was busy/stopped) is not integrated.
MAX_GAP = timedelta(minutes=2)
# Keep using the last known price this long when the price sensor drops out.
PRICE_MAX_AGE = timedelta(hours=2)

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 60

SETTING_INVESTMENT = "investment"
SETTING_BATTERY_COST = "battery_cost_excl_vat"

# Belastingdienst: adjustment period (herzieningsperiode) for a home battery.
VAT_ADJUSTMENT_YEARS = 5

MODE_NET_METERING = "net_metering"
MODE_NET_METERING_SURPLUS = "net_metering_surplus"
MODE_NO_NET_METERING = "no_net_metering"
MODE_NO_PRICE = "no_price"
PRICING_MODES = [
    MODE_NET_METERING,
    MODE_NET_METERING_SURPLUS,
    MODE_NO_NET_METERING,
    MODE_NO_PRICE,
]

SERVICE_SET_COUNTER = "set_counter"
