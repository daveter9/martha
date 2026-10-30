// Gas per 5-minute meter report over the last hour in m³/h, as a step line like the gas in
// martha-thermostat-graph.js. The area under the line has the colour of what the tap water
// detection chose (heat_gas in host/db/heat.sql): heating or hot water; a report split over
// two intervals shows both, stacked. The line has the colour of the larger part.
//
//   type: custom:martha-gas-split
//   entity: sensor.x      (attribute 'reeks': [[start epoch s, heating m³/h, hot water m³/h], ...])
//   hours: 1              (range shown; default 1)
//
// The series comes from the sensor (heat_gas_recent, polled every 30 seconds). The graph is
// redrawn every minute, so it moves on with time also when nothing changes.

const W = 640, H = 260, L = 44, R = 16, T = 34, B = 26;   // size and margins (viewBox units)
const MINUTE = 60e3, SLOT = 5 * MINUTE;
const COLORS = { space: "#8e24aa", dhw: "#00acc1" };

const fmt = (v, d) => v.toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d });

function nice(max, ticks) {
  // A tick step of 1, 2 or 5 x 10^n and the axis top rounded to it.
  const raw = max / ticks || 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((f) => f * p).find((s) => s >= raw);
  return { hi: Math.ceil(max / step) * step, step };
}

class MarthaGasSplit extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("entity is required");
    this._config = { hours: 1, ...config };
  }

  getCardSize() { return 5; }

  getGridOptions() { return { columns: "full", min_rows: 4 }; }

  connectedCallback() {
    this._timer = setInterval(() => this._render(), MINUTE);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  set hass(hass) {
    const s = hass.states[this._config.entity];
    const raw = s && s.attributes.reeks;
    if (raw === this._raw && this._card) return;
    this._raw = raw;
    try {
      this._data = (typeof raw === "string" ? JSON.parse(raw) : raw || [])
        .map(([t, space, dhw]) => [t * 1000, Number(space) || 0, Number(dhw) || 0]);
      this._error = s ? null : `${this._config.entity} bestaat niet`;
    } catch (e) {
      this._data = [];
      this._error = "Reeks niet te lezen";
    }
    this._render();
  }

  _render() {
    if (!this._data) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
    }
    const now = Date.now(), t0 = now - this._config.hours * 3600e3;
    const bars = this._data.filter(([t]) => t + SLOT > t0 && t <= now);
    const gy = nice(Math.max(0.1, ...bars.map(([, a, b]) => a + b)), 5);
    const x = (t) => L + ((Math.min(Math.max(t, t0), now) - t0) / (now - t0)) * (W - L - R);
    const y = (v) => T + (1 - v / gy.hi) * (H - T - B);

    let grid = "";
    for (let v = 0; v <= gy.hi + 1e-9; v += gy.step) {
      grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`
        + `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v, gy.step < 0.1 ? 2 : 1)}</text>`;
    }
    const first = new Date(t0); first.setMinutes(0, 0, 0);
    for (let t = first.getTime(); t <= now; t += 15 * MINUTE) {
      if (t <= t0) continue;
      const d = new Date(t);
      grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" class="grid"/>`
        + `<text x="${x(t)}" y="${H - B + 16}" text-anchor="middle">`
        + `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}</text>`;
    }

    // Per interval: the fill under the line (heating at the bottom, hot water on top), and the
    // line itself, with a riser from the previous level, in the colour of the larger part.
    let fill = "", line = "", prev = null;
    for (const [t, space, dhw] of bars) {
      const x1 = x(t), x2 = x(t + SLOT), top = space + dhw;
      const color = dhw > space ? COLORS.dhw : COLORS.space;
      if (space > 0) fill += `<rect x="${x1}" width="${x2 - x1}" y="${y(space)}" height="${y(0) - y(space)}" fill="${COLORS.space}" fill-opacity="0.25"/>`;
      if (dhw > 0) fill += `<rect x="${x1}" width="${x2 - x1}" y="${y(top)}" height="${y(space) - y(top)}" fill="${COLORS.dhw}" fill-opacity="0.25"/>`;
      const from = prev == null ? "" : `M${x1.toFixed(1)},${y(prev).toFixed(1)}V${y(top).toFixed(1)}`;
      line += `<path d="${from}${from ? "" : `M${x1.toFixed(1)},${y(top).toFixed(1)}`}H${x2.toFixed(1)}" fill="none" stroke="${color}" stroke-width="1.5"/>`;
      prev = top;
    }

    const sum = (i) => bars.reduce((s, b) => s + b[i], 0) / 12;   // m³/h per 5 minutes -> m³
    const legend = [["Verwarming", sum(1), COLORS.space], ["Tapwater", sum(2), COLORS.dhw]]
      .map(([name, v, color], i) =>
        `<g transform="translate(${[0, 190][i] + 8},14)"><rect x="0" y="-6" width="18" height="12" fill="${color}" fill-opacity="0.25"/>`
        + `<line x1="0" x2="18" y1="-6" y2="-6" stroke="${color}" stroke-width="1.5"/>`
        + `<text x="20" y="4">${name} <tspan font-weight="600">${fmt(v, 2)} m³</tspan></text></g>`).join("");

    this._card.innerHTML = `
      <style>
        .wrap { padding: 12px 12px 6px; }
        svg { width: 100%; height: auto; display: block; font: 12px var(--ha-font-family-body, sans-serif); }
        svg text { fill: var(--secondary-text-color); }
        svg g text { fill: var(--primary-text-color); }
        .grid { stroke: var(--divider-color); stroke-width: 1; }
        .err { color: var(--error-color); padding: 0 12px 8px; }
      </style>
      <div class="wrap"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Gas per 5 minuten: verwarming en tapwater">
        ${grid}${legend}${fill}${line}
        <text x="${L - 6}" y="${H - B + 16}" text-anchor="end">m³/h</text>
      </svg></div>
      ${this._error ? `<div class="err">${this._error}</div>` : ""}`;
  }
}

customElements.define("martha-gas-split", MarthaGasSplit);
window.customCards = window.customCards || [];
window.customCards.push({ type: "martha-gas-split", name: "Martha gas verdeeld",
  description: "Gas per 5 minuten, ingekleurd naar verwarming of tapwater" });
