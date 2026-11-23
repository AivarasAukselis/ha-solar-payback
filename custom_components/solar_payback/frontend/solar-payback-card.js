/*
 * Solar & Battery Payback card
 * Ships with the solar_payback integration and is loaded automatically.
 * Finds its entities by integration + translation key, so renaming entity IDs is fine.
 */

const CARD_VERSION = "1.3.0";
const PLATFORM = "solar_payback";

const KEYS = [
  "savings_today",
  "savings_this_month",
  "savings_this_year",
  "savings_total",
  "payback_progress",
  "payback_remaining",
  "payback_estimated_date",
  "battery_private_use_this_year",
  "vat_on_private_use_this_year",
  "battery_to_home_this_year",
  "battery_to_grid_this_year",
  "import_price",
  "export_value",
  "pricing_mode",
  "investment",
];

const MODE_LABEL = {
  net_metering: "Net metering",
  net_metering_surplus: "Net metering · surplus",
  no_net_metering: "No net metering",
  no_price: "No price",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

class SolarPaybackCard extends HTMLElement {
  static getStubConfig() {
    return { show_vat: true, show_prices: true };
  }

  static getConfigElement() {
    return document.createElement("solar-payback-card-editor");
  }

  setConfig(config) {
    this._config = { show_vat: true, show_prices: true, ...(config || {}) };
    this._signature = null;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 6;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  /* ---------- data ---------- */

  _findEntities() {
    const found = {};
    const overrides = this._config.entities || {};
    const registry = this._hass.entities || {};
    for (const [id, entry] of Object.entries(registry)) {
      if (entry.platform === PLATFORM && entry.translation_key && KEYS.includes(entry.translation_key)) {
        found[entry.translation_key] = id;
      }
    }
    return { ...found, ...overrides };
  }

  _state(key) {
    const id = this._ids[key];
    const st = id ? this._hass.states[id] : undefined;
    if (!st || st.state === "unknown" || st.state === "unavailable") return undefined;
    return st;
  }

  _num(key) {
    const st = this._state(key);
    if (!st) return undefined;
    const n = Number(st.state);
    return Number.isFinite(n) ? n : undefined;
  }

  _lang() {
    return this._hass.locale?.language || this._hass.language || navigator.language || "en";
  }

  _eur(value, digits = 2) {
    if (value === undefined) return "—";
    return new Intl.NumberFormat(this._lang(), {
      style: "currency",
      currency: "EUR",
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  }

  _pct(value, digits = 0) {
    if (value === undefined) return "—";
    return `${new Intl.NumberFormat(this._lang(), { maximumFractionDigits: digits }).format(value)}%`;
  }

  _price(value) {
    if (value === undefined) return "—";
    return new Intl.NumberFormat(this._lang(), {
      style: "currency",
      currency: "EUR",
      minimumFractionDigits: 3,
      maximumFractionDigits: 3,
    }).format(value);
  }

  _date(iso, opts) {
    if (!iso) return undefined;
    const d = new Date(`${String(iso).slice(0, 10)}T12:00:00`);
    if (Number.isNaN(d.getTime())) return undefined;
    return d.toLocaleDateString(this._lang(), opts);
  }

  /* ---------- render ---------- */

  _render() {
    if (!this._config || !this._hass) return;
    this._ids = this._findEntities();

    // Only re-render when one of our entities changed.
    const signature = KEYS.map((k) => {
      const st = this._ids[k] && this._hass.states[this._ids[k]];
      return st ? `${st.state}|${st.last_updated}` : "";
    }).join(";") + JSON.stringify(this._config);
    if (signature === this._signature) return;
    this._signature = signature;

    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.addEventListener("click", (ev) => {
        const target = ev.target.closest("[data-entity]");
        if (!target) return;
        const event = new Event("hass-more-info", { bubbles: true, composed: true });
        event.detail = { entityId: target.dataset.entity };
        this.dispatchEvent(event);
      });
    }

    if (!this._ids.savings_this_year) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="empty">
        Solar &amp; Battery Payback integration not found.<br>Add it under Settings → Devices &amp; services.
      </div></ha-card>`;
      return;
    }

    const c = this._config;
    const year = new Date().getFullYear();
    const saved = this._num("savings_this_year");
    const total = this._num("savings_total");
    const progress = this._num("payback_progress");
    const remaining = this._num("payback_remaining");
    const estimate = this._date(this._state("payback_estimated_date")?.state, { month: "short", year: "numeric" });
    const modeSt = this._state("pricing_mode");
    const mode = modeSt?.state;
    const nextChange = modeSt?.attributes?.next_rate_change;
    const nextDate = nextChange ? this._date(nextChange.from, { day: "numeric", month: "short", year: "numeric" }) : undefined;

    const neg = (v) => (v !== undefined && v < 0 ? " neg" : "");
    const link = (key) => (this._ids[key] ? ` data-entity="${esc(this._ids[key])}"` : "");

    // Payback
    let payback;
    if (progress === undefined) {
      payback = `<div class="hint"${link("investment")}>Set your investment on the device page to track payback →</div>`;
    } else {
      const width = Math.max(0, Math.min(progress, 100));
      const done = progress >= 100;
      payback = `
        <div class="row between">
          <span class="label">Payback</span>
          <span class="value strong"${link("payback_progress")}>${done ? "Paid back" : this._pct(progress, 1)}</span>
        </div>
        <div class="bar" role="progressbar" aria-valuenow="${width}" aria-valuemin="0" aria-valuemax="100">
          <div class="fill" style="width:${width}%"></div>
        </div>
        <div class="row between sub">
          <span${link("payback_remaining")}>${done ? "Investment earned back" : `${this._eur(remaining, 0)} to go`}</span>
          <span${link("payback_estimated_date")}>${estimate ? `est. ${esc(estimate)}` : "estimate after 7 days"}</span>
        </div>`;
    }

    // Battery private use (VAT)
    let vat = "";
    if (c.show_vat) {
      const pct = this._num("battery_private_use_this_year");
      const vatAmount = this._num("vat_on_private_use_this_year");
      const body =
        pct === undefined
          ? `<div class="sub waiting">No battery output measured yet this year</div>`
          : `<div class="split">
               <div class="home" style="width:${pct}%"></div>
               <div class="grid" style="width:${100 - pct}%"></div>
             </div>
             <div class="row between sub">
               <span${link("battery_to_home_this_year")}><i class="dot home"></i>House ${this._pct(pct)}</span>
               <span${link("battery_to_grid_this_year")}><i class="dot grid"></i>Grid ${this._pct(100 - pct)}</span>
             </div>`;
      vat = `
        <div class="section">
          <div class="row between">
            <span class="label">Battery use ${year}</span>
            ${pct === undefined ? "" : `<span class="value"${link("vat_on_private_use_this_year")}>VAT to report <b>${this._eur(vatAmount)}</b></span>`}
          </div>
          ${body}
        </div>`;
    }

    // Prices
    let prices = "";
    if (c.show_prices) {
      const bonus = this._state("export_value")?.attributes || {};
      const bonusNote = bonus.bonus_active
        ? `<span class="bonus">incl. ${esc((bonus.bonus_rules_active || []).join(" + ") || "bonus")} +${this._price(bonus.bonus_per_kwh)}</span>`
        : "";
      prices = `
        <div class="section prices">
          <div class="price"${link("import_price")}>
            <span class="label">Buy now</span>
            <span class="value">${this._price(this._num("import_price"))}<small>/kWh</small></span>
          </div>
          <div class="price"${link("export_value")}>
            <span class="label">Feed-in now</span>
            <span class="value">${this._price(this._num("export_value"))}<small>/kWh</small></span>
            ${bonusNote}
          </div>
        </div>
        ${nextDate ? `<div class="foot"${link("pricing_mode")}>Rates change ${esc(nextDate)}</div>` : ""}`;
    }

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="head">
          <span class="title">${esc(c.title || "Solar & Battery")}</span>
          ${mode ? `<span class="chip ${esc(mode)}"${link("pricing_mode")}>${esc(MODE_LABEL[mode] || mode)}</span>` : ""}
        </div>

        <div class="hero">
          <div class="big"${link("savings_this_year")}>
            <span class="label">Saved in ${year}</span>
            <span class="amount${neg(saved)}">${this._eur(saved)}</span>
            <span class="sub"${link("savings_total")}>${this._eur(total)} since start</span>
          </div>
          <div class="side">
            <div${link("savings_today")}><span class="label">Today</span><span class="value${neg(this._num("savings_today"))}">${this._eur(this._num("savings_today"))}</span></div>
            <div${link("savings_this_month")}><span class="label">This month</span><span class="value${neg(this._num("savings_this_month"))}">${this._eur(this._num("savings_this_month"))}</span></div>
          </div>
        </div>

        <div class="section">${payback}</div>
        ${vat}
        ${prices}
      </ha-card>`;
  }
}

const STYLE = `<style>
  :host { --sp-home: var(--energy-battery-out-color, #4db6ac); --sp-grid: var(--energy-grid-return-color, #8353d1); }
  ha-card { padding: 16px 16px 14px; overflow: hidden; }
  .empty { color: var(--secondary-text-color); padding: 8px 0; line-height: 1.5; }
  .head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 14px; }
  .title { font-size: 16px; font-weight: 500; color: var(--primary-text-color); letter-spacing: .1px; }
  .chip { font-size: 12px; line-height: 1; padding: 6px 10px; border-radius: 999px; cursor: pointer; white-space: nowrap;
          color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 12%, transparent); }
  .chip.no_net_metering { color: var(--secondary-text-color); background: color-mix(in srgb, var(--secondary-text-color) 12%, transparent); }
  .chip.no_price { color: var(--error-color, #db4437); background: color-mix(in srgb, var(--error-color, #db4437) 12%, transparent); }
  .label { font-size: 12px; color: var(--secondary-text-color); }
  .value { font-size: 14px; color: var(--primary-text-color); font-variant-numeric: tabular-nums; }
  .value b { font-weight: 600; }
  .strong { font-weight: 600; }
  .sub { font-size: 12px; color: var(--secondary-text-color); font-variant-numeric: tabular-nums; }
  .neg { color: var(--error-color, #db4437); }
  [data-entity] { cursor: pointer; }

  .hero { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; padding-bottom: 14px; }
  .big { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .amount { font-size: 34px; line-height: 1.1; font-weight: 400; color: var(--primary-text-color);
            font-variant-numeric: tabular-nums; letter-spacing: -.5px; white-space: nowrap; }
  .side { display: flex; flex-direction: column; gap: 8px; text-align: right; flex-shrink: 0; }
  .side > div { display: flex; flex-direction: column; gap: 1px; }

  .section { border-top: 1px solid var(--divider-color); padding: 12px 0 12px; }
  .section:last-of-type { padding-bottom: 2px; }
  .waiting { padding: 6px 0 0; }
  .row { display: flex; align-items: baseline; }
  .between { justify-content: space-between; gap: 12px; }
  .bar, .split { height: 8px; border-radius: 4px; margin: 8px 0 6px; overflow: hidden;
                 background: color-mix(in srgb, var(--secondary-text-color) 15%, transparent); }
  .fill { height: 100%; border-radius: 4px; background: var(--primary-color); transition: width .6s ease; }
  .split { display: flex; gap: 2px; background: transparent; }
  .split > div { height: 100%; transition: width .6s ease; }
  .split .home { background: var(--sp-home); border-radius: 4px 0 0 4px; }
  .split .grid { background: var(--sp-grid); border-radius: 0 4px 4px 0; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: 0; }
  .dot.home { background: var(--sp-home); }
  .dot.grid { background: var(--sp-grid); }
  .hint { font-size: 13px; color: var(--primary-color); padding: 2px 0 6px; }

  .prices { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding-bottom: 0; }
  .price { display: flex; flex-direction: column; gap: 2px; }
  .price:last-child { text-align: right; }
  .price small { font-size: 11px; color: var(--secondary-text-color); margin-left: 2px; }
  .bonus { font-size: 11px; color: var(--success-color, #43a047); }
  .foot { margin-top: 10px; font-size: 12px; color: var(--secondary-text-color); }
</style>`;

/* ---------- visual editor ---------- */

class SolarPaybackCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (s) =>
        ({ title: "Title", show_vat: "Show battery use and VAT", show_prices: "Show current prices" })[s.name] || s.name;
      this._form.addEventListener("value-changed", (ev) => {
        const event = new Event("config-changed", { bubbles: true, composed: true });
        event.detail = { config: { ...this._config, ...ev.detail.value } };
        this.dispatchEvent(event);
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = [
      { name: "title", selector: { text: {} } },
      { name: "show_vat", selector: { boolean: {} } },
      { name: "show_prices", selector: { boolean: {} } },
    ];
    this._form.data = { show_vat: true, show_prices: true, ...this._config };
  }
}

if (!customElements.get("solar-payback-card")) {
  customElements.define("solar-payback-card", SolarPaybackCard);
  customElements.define("solar-payback-card-editor", SolarPaybackCardEditor);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "solar-payback-card",
    name: "Solar & Battery Payback",
    description: "Savings, payback progress, battery VAT private use and current prices.",
    preview: true,
    documentationURL: "https://github.com/AivarasAukselis/ha-solar-payback",
  });
  console.info(`%c SOLAR-PAYBACK-CARD %c ${CARD_VERSION} `, "background:#4db6ac;color:#fff;border-radius:3px 0 0 3px", "background:#555;color:#fff;border-radius:0 3px 3px 0");
}
