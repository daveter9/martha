// The thermostat over the last hours in one graph: room temperature, setpoint and the outdoor
// temperature on the left axis (°C), gas for heating on the right axis (m³/h). HA's history-graph cannot combine a
// climate entity and a sensor, nor show a second y-axis (DESIGN.md, C25).
//
//   type: custom:martha-thermostat-graph
//   climate: climate.x      (current_temperature and temperature attributes)
//   gas: sensor.y           (gas for heating now, m³/h; optional)
//   outdoor: sensor.z       (outdoor temperature, °C; optional)
//   hours: 1                (range shown; default 24)
//
// History comes from HA's websocket (history/history_during_period, the recorder). New
// states are added as they arrive; the graph is redrawn every minute, so it moves on with
// time also when nothing changes, and the history is fetched again every 15 minutes.

const W = 640, H = 290, L = 44, R = 52, T = 44, B = 26;   // size and margins (viewBox units)
const MINUTE = 60e3, REFETCH = 15 * MINUTE;
const COLORS = { temp: "#1e88e5", set: "#e8710a", gas: "#8e24aa", out: "#43a047" };

const fmt = (v, d) => v.toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d });

function nice(min, max, ticks) {
  // A tick step of 1, 2 or 5 x 10^n and the axis range rounded to it.
  const raw = (max - min) / ticks || 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((f) => f * p).find((s) => s >= raw);
  return { lo: Math.floor(min / step) * step, hi: Math.ceil(max / step) * step, step };
}

class MarthaThermostatGraph extends HTMLElement {
  setConfig(config) {
    if (!config.climate) throw new Error("climate is required");
    this._config = { hours: 24, ...config };
    this._series = null;
  }

  getCardSize() { return 5; }

  getGridOptions() { return { columns: "full", min_rows: 4 }; }

  connectedCallback() {
    this._timer = setInterval(() => this._tick(), MINUTE);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._series || Date.now() - this._fetched > REFETCH) {
      if (!this._loading) this._load();
      return;
    }
    // Live: add a point when an entity got a new state.
    let changed = false;
    for (const [entity, add] of this._live()) {
      const s = hass.states[entity];
      if (s && s.last_updated !== this._seen[entity]) {
        this._seen[entity] = s.last_updated;
        add(Date.parse(s.last_updated), s);
        changed = true;
      }
    }
    if (changed) this._render();
  }

  _live() {
    const { climate, gas, outdoor } = this._config;
    const out = [[climate, (t, s) => this._addClimate(t, s.attributes)]];
    if (gas) out.push([gas, (t, s) => this._addValue("gas", t, s.state)]);
    if (outdoor) out.push([outdoor, (t, s) => this._addValue("out", t, s.state)]);
    return out;
  }

  _addClimate(t, a) {
    const temp = Number(a.current_temperature), set = Number(a.temperature);
    if (a.current_temperature != null && isFinite(temp)) this._series.temp.push([t, temp]);
    if (a.temperature != null && isFinite(set)) this._series.set.push([t, set]);
  }

  _addValue(series, t, state) {
    const v = Number(state);
    if (state !== "" && state != null && isFinite(v)) this._series[series].push([t, v]);
  }

  async _load() {
    this._loading = true;
    const { climate, gas, outdoor, hours } = this._config;
    const end = new Date(), start = new Date(end - hours * 3600e3);
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period", start_time: start.toISOString(),
        end_time: end.toISOString(), entity_ids: [climate, gas, outdoor].filter(Boolean),
        minimal_response: false, no_attributes: false, significant_changes_only: false,
      });
      this._series = { temp: [], set: [], gas: [], out: [] };
      this._seen = {};
      for (const st of res[climate] || []) {
        if (st.a) this._lastAttrs = st.a;
        this._addClimate(st.lu * 1000, st.a || this._lastAttrs || {});
      }
      for (const st of (gas && res[gas]) || []) this._addValue("gas", st.lu * 1000, st.s);
      for (const st of (outdoor && res[outdoor]) || []) this._addValue("out", st.lu * 1000, st.s);
      for (const [entity] of this._live()) {
        const s = this._hass.states[entity];
        if (s) this._seen[entity] = s.last_updated;
      }
      this._fetched = Date.now();
      this._error = null;
    } catch (e) {
      this._error = e.message || String(e);
      this._fetched = Date.now();   // try again at the next refetch, not on every state
      this._series = this._series || { temp: [], set: [], gas: [], out: [] };
    }
    this._loading = false;
    this._render();
  }

  _tick() {
    if (this._series && Date.now() - this._fetched > REFETCH) this._load();
    else this._render();
  }

  _render() {
    if (!this._series) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
    }
    const now = Date.now(), t0 = now - this._config.hours * 3600e3;
    // Each series as steps from t0 to now: the value before t0 carries over, the last value
    // runs on until now.
    const clip = (pts) => {
      const before = pts.filter(([t]) => t <= t0).pop();
      const inside = pts.filter(([t]) => t > t0 && t <= now);
      return (before ? [[t0, before[1]]] : []).concat(inside);
    };
    const temp = clip(this._series.temp), set = clip(this._series.set);
    const gas = this._config.gas ? clip(this._series.gas) : [];
    const out = this._config.outdoor ? clip(this._series.out) : [];

    const temps = temp.concat(set, out).map((p) => p[1]);
    const ty = temps.length ? nice(Math.min(...temps) - 0.5, Math.max(...temps) + 0.5, 12)
                            : { lo: 15, hi: 22, step: 1 };
    const gy = nice(0, Math.max(0.1, ...gas.map((p) => p[1])), 5);
    const x = (t) => L + ((t - t0) / (now - t0)) * (W - L - R);
    const yT = (v) => T + (1 - (v - ty.lo) / (ty.hi - ty.lo)) * (H - T - B);
    const yG = (v) => T + (1 - (v - gy.lo) / (gy.hi - gy.lo)) * (H - T - B);
    const steps = (pts, y) => pts.length
      ? pts.map(([t, v], i) => (i ? `H${x(t).toFixed(1)}V${y(v).toFixed(1)}` : `M${x(t).toFixed(1)},${y(v).toFixed(1)}`)).join("")
        + `H${x(now).toFixed(1)}`
      : "";

    let grid = "";
    for (let v = ty.lo; v <= ty.hi + 1e-9; v += ty.step) {
      grid += `<line x1="${L}" x2="${W - R}" y1="${yT(v)}" y2="${yT(v)}" class="grid"/>`
        + `<text x="${L - 6}" y="${yT(v) + 4}" text-anchor="end" fill="${COLORS.temp}">${fmt(v, ty.step < 1 ? 1 : 0)}</text>`;
    }
    if (this._config.gas) {
      for (let v = gy.lo; v <= gy.hi + 1e-9; v += gy.step) {
        grid += `<text x="${W - R + 6}" y="${yG(v) + 4}" fill="${COLORS.gas}">${fmt(v, gy.step < 0.1 ? 2 : 1)}</text>`;
      }
    }
    // Time axis: a line per step and a label per 'label' minutes, by the range shown.
    const h = this._config.hours;
    const [step, label] = h <= 1 ? [15, 15] : h <= 3 ? [15, 30] : h <= 12 ? [60, 60] : [60, 180];
    const first = new Date(t0); first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= now; t += step * MINUTE) {
      if (t <= t0) continue;
      const d = new Date(t), mins = d.getHours() * 60 + d.getMinutes();
      grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" class="grid"/>`;
      if (mins % label === 0) {
        grid += `<text x="${x(t)}" y="${H - B + 16}" text-anchor="middle">`
          + `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}</text>`;
      }
    }

    const last = (pts) => (pts.length ? pts[pts.length - 1][1] : null);
    const legend = [
      ["Huidige temperatuur", last(temp), "°C", 1, COLORS.temp, ""],
      ["Doeltemperatuur", last(set), "°C", 1, COLORS.set, "4 3"],
      ...(this._config.outdoor ? [["Buiten", last(out), "°C", 1, COLORS.out, ""]] : []),
      ...(this._config.gas ? [["Gas verwarming", last(gas), "m³/h", 2, COLORS.gas, ""]] : []),
    ].map(([name, v, unit, d, color, dash], i) =>
      // Two rows of two.
      `<g transform="translate(${(i % 2) * 300 + 8},${14 + Math.floor(i / 2) * 18})"><line x1="0" x2="18" y1="0" y2="0" stroke="${color}" stroke-width="2.5" stroke-dasharray="${dash}"/>`
      + `<text x="24" y="4">${name} <tspan font-weight="600">${v == null ? "–" : fmt(v, d)} ${unit}</tspan></text></g>`).join("");

    const gasArea = gas.length
      ? `<path d="${steps(gas, yG)}V${yG(0)}H${x(gas[0][0])}Z" fill="${COLORS.gas}" fill-opacity="0.15"/>`
        + `<path d="${steps(gas, yG)}" fill="none" stroke="${COLORS.gas}" stroke-width="1.5"/>`
      : "";
    this._card.innerHTML = `
      <style>
        .wrap { padding: 12px 12px 6px; }
        svg { width: 100%; height: auto; display: block; font: 12px var(--ha-font-family-body, sans-serif); }
        svg text { fill: var(--secondary-text-color); }
        svg g text { fill: var(--primary-text-color); }
        .grid { stroke: var(--divider-color); stroke-width: 1; }
        .err { color: var(--error-color); padding: 0 12px 8px; }
      </style>
      <div class="wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Thermostaat en gas verwarming">
        ${grid}${legend}
        ${gasArea}
        <path d="${steps(set, yT)}" fill="none" stroke="${COLORS.set}" stroke-width="2" stroke-dasharray="4 3"/>
        ${out.length ? `<path d="${steps(out, yT)}" fill="none" stroke="${COLORS.out}" stroke-width="2"/>` : ""}
        <path d="${steps(temp, yT)}" fill="none" stroke="${COLORS.temp}" stroke-width="2.5"/>
        <text x="${L - 6}" y="${H - B + 16}" text-anchor="end" fill="${COLORS.temp}">°C</text>
        ${this._config.gas ? `<text x="${W - R + 6}" y="${H - B + 16}" fill="${COLORS.gas}">m³/h</text>` : ""}
      </svg></div>
      ${this._error ? `<div class="err">Geschiedenis niet geladen: ${this._error}</div>` : ""}`;
  }
}

customElements.define("martha-thermostat-graph", MarthaThermostatGraph);
window.customCards = window.customCards || [];
window.customCards.push({ type: "martha-thermostat-graph", name: "Martha thermostaat",
  description: "Temperatuur, ingestelde temperatuur, buitentemperatuur en gas voor verwarming in één grafiek" });
