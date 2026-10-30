// Gas per 5-minute meter report in m³/h, as a step line like the gas in
// martha-thermostat-graph.js. The area under the line has the colour of what the tap water
// detection chose (heat_gas in host/db/heat.sql): heating or hot water; a report split over
// two intervals shows both, stacked. The line has the colour of the larger part.
//
//   type: custom:martha-gas-split
//   entity: sensor.x          (attribute 'reeks': [[start epoch s, heating m³/h, hot water m³/h], ...])
//   hours: 1                  (range shown at first; default 1)
//   height: 240               (px)
//   navigation_path: /a/b     (optional: a tap opens that page; otherwise the graph is interactive)
//
// The sensor (heat_gas_recent, polled every 30 seconds) holds the last hour; the recorder keeps
// its states for purge_keep_days (10 days). The interactive graph fetches those states for the
// range shown and joins them, the newest per interval winning. Like HA's graphs: zoom with the
// scroll wheel or two fingers, drag to move through time, hover or tap for the values; the
// buttons zoom and go back to now. The graph is redrawn every minute, so it moves on with time.

const L = 44, R = 16, T = 34, B = 26;   // margins (px)
const MINUTE = 60e3, SLOT = 5 * MINUTE, HOUR = 60 * MINUTE, DAY = 24 * HOUR;
const MIN_SPAN = 15 * MINUTE, MAX_SPAN = 7 * DAY;
const COLORS = { space: "#8e24aa", dhw: "#00acc1" };

const fmt = (v, d) => v.toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d });
const pad = (n) => String(n).padStart(2, "0");
const hhmm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const ddmm = (d) => `${pad(d.getDate())}-${pad(d.getMonth() + 1)}`;

function nice(max, ticks) {
  // A tick step of 1, 2 or 5 x 10^n and the axis top rounded to it.
  const raw = max / ticks || 1;
  const p = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((f) => f * p).find((s) => s >= raw);
  return { hi: Math.ceil(max / step) * step, step };
}

function series(raw) {
  // The attribute as [[ms, heating, hot water], ...].
  const list = typeof raw === "string" ? JSON.parse(raw) : raw || [];
  return list.map(([t, space, dhw]) => [t * 1000, Number(space) || 0, Number(dhw) || 0]);
}

class MarthaGasSplit extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("entity is required");
    this._config = { hours: 1, height: 240, ...config };
    this._span = this._config.hours * HOUR;
    this._end = null;              // end of the range shown; null = now
    this._slots = new Map();       // interval start (ms) -> [heating, hot water]
    this._from = null;             // history fetched from here on (ms)
    this._done = false;            // nothing older in the recorder
    this._pointers = new Map();
  }

  get _interactive() { return !this._config.navigation_path; }

  getCardSize() { return Math.ceil(this._config.height / 50); }

  getGridOptions() { return { columns: "full", min_rows: Math.ceil(this._config.height / 56) }; }

  connectedCallback() {
    this._timer = setInterval(() => this._render(), MINUTE);
    this._resize = new ResizeObserver(() => this._render());
    this._resize.observe(this);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._resize.disconnect();
  }

  set hass(hass) {
    this._hass = hass;
    const s = hass.states[this._config.entity];
    const raw = s && s.attributes.reeks;
    if (raw === this._raw && this._card) return;
    this._raw = raw;
    try {
      for (const [t, a, b] of series(raw)) this._slots.set(t, [a, b]);   // the newest wins
      this._error = s ? null : `${this._config.entity} bestaat niet`;
    } catch (e) {
      this._error = "Reeks niet te lezen";
    }
    if (this._from == null) this._from = Date.now() - HOUR;
    this._render();
  }

  // --- History ------------------------------------------------------------------------------

  async _fetch(start) {
    // States of the sensor from 'start' to what we have, joined into the slots. Newer data
    // (already in the slots) wins over what this batch says about the same interval.
    if (this._loading || this._done || !this._hass) return;
    this._loading = true;
    const end = this._from;
    try {
      const res = await this._hass.callWS({
        type: "history/history_during_period", start_time: new Date(start).toISOString(),
        end_time: new Date(end).toISOString(), entity_ids: [this._config.entity],
        minimal_response: false, no_attributes: false, significant_changes_only: false,
      });
      const batch = new Map();
      let attrs = null, n = 0;
      for (const st of res[this._config.entity] || []) {
        if (st.a) attrs = st.a;
        if (!attrs || attrs.reeks == null) continue;
        n++;
        for (const [t, a, b] of series(attrs.reeks)) batch.set(t, [a, b]);
      }
      for (const [t, v] of batch) if (!this._slots.has(t)) this._slots.set(t, v);
      this._from = start;
      if (!n) this._done = true;   // before the sensor existed, or purged
    } catch (e) {
      this._error = `Geschiedenis niet geladen: ${e.message || e}`;
      this._done = true;
    }
    this._loading = false;
    this._render();
  }

  // --- Interaction --------------------------------------------------------------------------

  _range() {
    const now = Date.now();
    const end = this._end == null ? now : Math.min(this._end, now);
    return [end - this._span, end, now];
  }

  _setRange(start, span) {
    const now = Date.now();
    span = Math.min(MAX_SPAN, Math.max(MIN_SPAN, span));
    let end = start + span;
    this._span = span;
    this._end = end >= now - MINUTE ? null : end;
    this._render();
  }

  _zoom(factor, atX) {
    const [t0, t1] = this._range();
    const plot = this._w - L - R;
    const f = atX == null ? 0.5 : Math.min(1, Math.max(0, (atX - L) / plot));
    const anchor = t0 + f * (t1 - t0);
    const span = Math.min(MAX_SPAN, Math.max(MIN_SPAN, this._span * factor));
    this._setRange(anchor - f * span, span);
  }

  _x(e) {
    return e.clientX - this._card.querySelector("svg").getBoundingClientRect().left;
  }

  _listen() {
    const c = this._card;
    if (!this._interactive) {
      c.style.cursor = "pointer";
      c.addEventListener("click", () => {
        history.pushState(null, "", this._config.navigation_path);
        window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: false } }));
      });
      return;
    }
    c.addEventListener("wheel", (e) => {
      if (!e.target.closest("svg")) return;
      e.preventDefault();
      this._zoom(e.deltaY > 0 ? 1.25 : 0.8, this._x(e));
    }, { passive: false });
    c.addEventListener("pointerdown", (e) => {
      if (!e.target.closest("svg")) return;
      e.target.setPointerCapture(e.pointerId);
      this._pointers.set(e.pointerId, this._x(e));
      const [t0] = this._range();
      this._drag = { x: this._x(e), start: t0, span: this._span, moved: false,
                     dist: this._pinchDist() };
    });
    c.addEventListener("pointermove", (e) => {
      const x = this._x(e);
      if (!this._pointers.has(e.pointerId)) {
        if (e.pointerType === "mouse") { this._hover = x; this._render(); }
        return;
      }
      this._pointers.set(e.pointerId, x);
      const d = this._drag, plot = this._w - L - R;
      if (this._pointers.size >= 2 && d.dist) {
        // Pinch: zoom around the middle of the two fingers.
        const xs = [...this._pointers.values()], mid = (xs[0] + xs[1]) / 2;
        const span = d.span * d.dist / Math.max(10, this._pinchDist());
        const f = Math.min(1, Math.max(0, (mid - L) / plot));
        this._setRange(d.start + f * d.span - f * span, span);
        d.moved = true;
      } else if (Math.abs(x - d.x) > 4 || d.moved) {
        d.moved = true;
        this._hover = null;
        this._setRange(d.start - ((x - d.x) / plot) * d.span, d.span);
      }
    });
    const up = (e) => {
      const d = this._drag;
      this._pointers.delete(e.pointerId);
      if (this._pointers.size) {
        const [t0] = this._range();
        this._drag = { x: [...this._pointers.values()][0], start: t0, span: this._span, moved: true, dist: null };
        return;
      }
      if (d && !d.moved) { this._hover = this._x(e); this._render(); }   // a tap shows the values
      this._drag = null;
    };
    c.addEventListener("pointerup", up);
    c.addEventListener("pointercancel", up);
    c.addEventListener("pointerleave", (e) => {
      if (e.pointerType === "mouse" && !this._pointers.size) { this._hover = null; this._render(); }
    });
    c.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const act = b.dataset.act;
      if (act === "in") this._zoom(0.5);
      if (act === "out") this._zoom(2);
      if (act === "now") { this._span = this._config.hours * HOUR; this._end = null; this._render(); }
    });
  }

  _pinchDist() {
    const xs = [...this._pointers.values()];
    return xs.length >= 2 ? Math.abs(xs[0] - xs[1]) : null;
  }

  // --- Drawing ------------------------------------------------------------------------------

  _ticks(t0, t1, plot) {
    // A line per step; the step from the range and the width, aligned to local time.
    const steps = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880].map((m) => m * MINUTE);
    const step = steps.find((s) => (t1 - t0) / s <= plot / 70) || steps[steps.length - 1];
    const d = new Date(t0); d.setHours(0, 0, 0, 0);
    const out = [];
    for (let t = d.getTime(); t <= t1; t += step) {
      if (t <= t0) continue;
      const at = new Date(t);
      const midnight = at.getHours() === 0 && at.getMinutes() === 0;
      out.push([t, step >= DAY || (midnight && t1 - t0 > 6 * HOUR) ? ddmm(at) : hhmm(at)]);
    }
    return out;
  }

  _render() {
    if (!this._hass) return;
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
      this._listen();
    }
    const W = this._w = Math.max(280, this.clientWidth || 640) - 24, H = this._config.height;
    const [t0, t1] = this._range();
    if (this._interactive && this._from != null && t0 < this._from) this._fetch(t0 - this._span);

    const bars = [...this._slots].filter(([t]) => t + SLOT > t0 && t < t1)
      .sort((a, b) => a[0] - b[0]).map(([t, [a, b]]) => [t, a, b]);
    const gy = nice(Math.max(0.1, ...bars.map(([, a, b]) => a + b)), 5);
    const plot = W - L - R;
    const x = (t) => L + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * plot;
    const y = (v) => T + (1 - v / gy.hi) * (H - T - B);

    let grid = "";
    for (let v = 0; v <= gy.hi + 1e-9; v += gy.step) {
      grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="grid"/>`
        + `<text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v, gy.step < 0.1 ? 2 : 1)}</text>`;
    }
    for (const [t, label] of this._ticks(t0, t1, plot)) {
      grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${H - B}" class="grid"/>`
        + `<text x="${x(t)}" y="${H - B + 16}" text-anchor="middle">${label}</text>`;
    }

    // Per interval: the fill under the line (heating at the bottom, hot water on top), and the
    // line itself, with a riser from the previous level, in the colour of the larger part.
    const fill = { space: "", dhw: "" }, line = { space: "", dhw: "" };
    let prev = null, prevT = null;
    for (const [t, space, dhw] of bars) {
      const x1 = x(t).toFixed(1), x2 = x(t + SLOT).toFixed(1), top = space + dhw, y0 = y(0).toFixed(1);
      if (space > 0) fill.space += `M${x1},${y0}V${y(space).toFixed(1)}H${x2}V${y0}Z`;
      if (dhw > 0) fill.dhw += `M${x1},${y(space).toFixed(1)}V${y(top).toFixed(1)}H${x2}V${y(space).toFixed(1)}Z`;
      const from = prev != null && prevT === t - SLOT ? y(prev) : y(top);
      line[dhw > space ? "dhw" : "space"] += `M${x1},${from.toFixed(1)}V${y(top).toFixed(1)}H${x2}`;
      prev = top; prevT = t;
    }
    const paths = ["space", "dhw"].map((k) =>
      `<path d="${fill[k]}" fill="${COLORS[k]}" fill-opacity="0.25"/>`).join("")
      + ["space", "dhw"].map((k) =>
      `<path d="${line[k]}" fill="none" stroke="${COLORS[k]}" stroke-width="1.5"/>`).join("");

    // Legend: the total of each part in the range shown.
    const sum = (i) => bars.reduce((s, b) => s + b[i], 0) / 12;   // m³/h per 5 minutes -> m³
    const legend = [["Verwarming", sum(1), COLORS.space], ["Tapwater", sum(2), COLORS.dhw]]
      .map(([name, v, color], i) =>
        `<g transform="translate(${[0, 190][i] + 8},14)"><rect x="0" y="-6" width="18" height="12" fill="${color}" fill-opacity="0.25"/>`
        + `<line x1="0" x2="18" y1="-6" y2="-6" stroke="${color}" stroke-width="1.5"/>`
        + `<text x="24" y="4">${name} <tspan font-weight="600">${fmt(v, 2)} m³</tspan></text></g>`).join("");

    // Tooltip: the interval under the pointer.
    let tip = "";
    if (this._interactive && this._hover != null && this._hover >= L && this._hover <= W - R) {
      const at = t0 + ((this._hover - L) / plot) * (t1 - t0);
      const slot = Math.floor(at / SLOT) * SLOT, v = this._slots.get(slot);
      const xs = x(slot + SLOT / 2), w = 190, left = xs + 10 + w > W - R ? xs - 10 - w : xs + 10;
      tip = `<line x1="${xs}" x2="${xs}" y1="${T}" y2="${H - B}" class="cursor"/>`
        + `<g transform="translate(${left},${T + 4})" class="tip"><rect width="${w}" height="62" rx="6"/>`
        + `<text x="10" y="18" font-weight="600">${ddmm(new Date(slot))} ${hhmm(new Date(slot))}–${hhmm(new Date(slot + SLOT))}</text>`
        + (v ? `<text x="10" y="36" fill="${COLORS.space}">Verwarming ${fmt(v[0], 3)} m³/h</text>`
             + `<text x="10" y="53" fill="${COLORS.dhw}">Tapwater ${fmt(v[1], 3)} m³/h</text>`
             : `<text x="10" y="36">Geen gegevens</text>`)
        + `</g>`;
    }

    const zoomed = this._end != null || this._span !== this._config.hours * HOUR;
    const buttons = this._interactive
      ? `<div class="bar">
           <span class="hint">${this._loading ? "Geschiedenis laden…" : this._done && t0 < this._from ? "Oudere gegevens zijn er niet" : ""}</span>
           <button data-act="out" title="Uitzoomen">−</button>
           <button data-act="in" title="Inzoomen">+</button>
           <button data-act="now" title="Terug naar nu" ${zoomed ? "" : "disabled"}>Nu</button>
         </div>`
      : "";

    this._card.innerHTML = `
      <style>
        .wrap { padding: 12px 12px 6px; }
        svg { display: block; font: 12px var(--ha-font-family-body, sans-serif); user-select: none;
              ${this._interactive ? "touch-action: none; cursor: grab;" : ""} }
        svg text { fill: var(--secondary-text-color); }
        svg g text { fill: var(--primary-text-color); }
        .grid { stroke: var(--divider-color); stroke-width: 1; }
        .cursor { stroke: var(--secondary-text-color); stroke-width: 1; stroke-dasharray: 3 3; }
        .tip rect { fill: var(--card-background-color, #fff); stroke: var(--divider-color); }
        .bar { display: flex; gap: 6px; justify-content: flex-end; align-items: center; padding: 0 12px 10px; }
        .bar .hint { flex: 1; color: var(--secondary-text-color); font-size: 12px; }
        .bar button { min-width: 36px; height: 32px; border-radius: 8px; border: 1px solid var(--divider-color);
                      background: none; color: var(--primary-text-color); font: inherit; cursor: pointer; }
        .bar button:disabled { opacity: 0.4; cursor: default; }
        .err { color: var(--error-color); padding: 0 12px 8px; }
      </style>
      <div class="wrap"><svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Gas per 5 minuten: verwarming en tapwater">
        ${grid}${legend}${paths}${tip}
        <text x="${L - 6}" y="${H - B + 16}" text-anchor="end">m³/h</text>
      </svg></div>
      ${buttons}
      ${this._error ? `<div class="err">${this._error}</div>` : ""}`;
  }
}

customElements.define("martha-gas-split", MarthaGasSplit);
window.customCards = window.customCards || [];
window.customCards.push({ type: "martha-gas-split", name: "Martha gas verdeeld",
  description: "Gas per 5 minuten, ingekleurd naar verwarming of tapwater" });
