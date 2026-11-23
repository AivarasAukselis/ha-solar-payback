# Solar & Battery Payback (Home Assistant custom integration)

Tracks, on one **Solar & Battery** device:

- **Savings** today / this month / this year / total: what you would pay without
  solar + battery minus what you actually pay, using live dynamic prices.
- **Payback**: progress %, amount still to earn back, rough estimated payback date.
- **Battery VAT private use** (Dutch *privégebruik thuisbatterij*): share of battery
  output used by the house vs. sent to the grid, and the VAT to report
  (battery cost excl. VAT / 5 x private % x VAT), only within the 5-year
  adjustment period. Last year's figures are kept as attributes after New Year.
- **Dated rate schedule**: energy tax, supplier purchase fee, feed-in fee, VAT
  and net metering (saldering) per period. Add announced changes in advance;
  they apply automatically on their date. Net metering is on until
  31 Dec 2026 and off from 1 Jan 2027 by default.

**How energy is measured**

- Power sensors are integrated on **every state change** (previous reading held
  until the next one, like Home Assistant's own integration helper), plus a
  10 s heartbeat while readings stay constant. No reading is skipped.
- Optional **P1 kWh counters** for grid import/export: the smart meter counts
  continuously (1 Wh resolution) and nets the three phases exactly like your
  supplier bills. When set, grid import/export and the money use these.
  If the P1 meter drops out, it falls back to the power sensors and
  re-baselines when it returns (no double counting). Attribute
  `grid_energy_source` on *Pricing mode* shows what is used.
- The battery split always uses power, because it needs battery output and
  export at the same moment. Use the Solis grid export power for this (same
  Modbus poll as the battery power, so the readings line up).

Counters are stored on disk (survive restarts); entities update once a minute.

## Install

1. Copy `custom_components/solar_payback` to `/config/custom_components/solar_payback`.
2. Restart Home Assistant.
3. Settings > Devices & services > Add integration > **Solar & Battery Payback**.

## Setup values for a Solis S6-EH3P (Solis Modbus) + EnergyZero

| Field | Entity |
|---|---|
| Battery discharge power | `sensor.general_house_solis_s6_eh3p_battery_discharge_power` |
| Grid import power | `sensor.general_house_solis_s6_eh3p_grid_import_power` |
| Grid export power | `sensor.general_house_solis_s6_eh3p_grid_export_power` |
| House consumption power | `sensor.general_house_solis_s6_eh3p_household_load_power` |
| Grid import kWh counters | `sensor.slimmelezer_energy_consumed_tariff_1`, `sensor.slimmelezer_energy_consumed_tariff_2` |
| Grid export kWh counters | `sensor.slimmelezer_energy_produced_tariff_1`, `sensor.slimmelezer_energy_produced_tariff_2` |
| Spot price sensor | `sensor.energyzero_today_energy_current_hour_price` (includes VAT: ON) |
| I keep the VAT on feed-in income | OFF if VAT-registered for the battery |
| Year the battery was put into use | e.g. 2026 |

Check the spot price: the integration's **Import price (all-in)** should match
your supplier's all-in price for the current hour.

Then fill in the two numbers on the device page:
**Investment (after VAT refund)** and **Battery cost excl. VAT**.

## Rate schedule

Edit later via the integration's **Configure** button. Each period only needs
the fields that change. Amounts are EUR/kWh **excluding VAT**.

```yaml
- from: '2026-01-01'
  energy_tax: 0.09161          # 0.11085 incl. VAT
  purchase_fee: 0.0182         # your supplier's inkoopvergoeding
  feed_in_fee: 0.0             # terugleverkosten
  vat: 0.21
  net_metering: true
  net_metering_includes_purchase_fee: false
- from: '2027-01-01'
  energy_tax: 0.08802          # 0.1065 incl. VAT (Belastingplan 2027, corrected)
  feed_in_fee: 0.02            # example: check your supplier's 2027 terms
  net_metering: false
```

## Price model

- Import: (spot + purchase fee + energy tax) x (1 + VAT)
- Feed-in, net metering active (this year's export below this year's import):
  spot income + (energy tax [+ purchase fee]) x (1 + VAT) - feed-in fee x (1 + VAT)
- Feed-in otherwise: spot income - feed-in fee x (1 + VAT)
- Spot income includes VAT only if "I keep the VAT on feed-in income" is on.

Battery split: solar feeds the house first; export while the battery
discharges counts as battery-to-grid, up to the discharge power.

## Service `solar_payback.set_counter`

Set any counter, e.g. add savings or grid import/export from earlier in the
year (the net metering cap uses this year's import/export):

```yaml
action: solar_payback.set_counter
data:
  counter: grid_import_year
  value: 2350
```

## Dashboard

Paste `dashboard_card.yaml` into a Manual card.

## Releases

Bump `version` in `custom_components/solar_payback/manifest.json` and push to
`main`. The GitHub Action runs the tests and, if they pass and the version is
new, creates tag `vX.Y.Z` and a release. The Marketplace then offers the update.

## Tests

```bash
pip install -r requirements_test.txt
pytest
```

Not tax advice: the private-use method follows the Belastingdienst example
but is not an officially approved calculation.
