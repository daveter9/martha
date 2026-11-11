// Power flow now, as a small sankey: grid and solar on the left, home and feed-in on the
// right. Unlike HA's power-sankey it has no "untracked consumption" (DESIGN.md, C25).
//
//   type: custom:martha-power-flow
//   title: ...
//   grid_import: sensor.x   (power from the grid, W or kW)
//   grid_export: sensor.y   (power to the grid, optional)
//   solar: sensor.z         (power of the panels, optional)
//
// Without a solar sensor, feed-in still means the panels produce: they are shown with the
// feed-in as a lower bound ("≥"), and so is the home, since its share of the panels is unknown.

const W = 400, H = 170, NODE = 10, GAP = 14, TOP = 18, LABEL = 4;

class MarthaPowerFlow extends HTMLElement {
  setConfig(config) {
    if (!config.grid_import) throw new Error("grid_import is required");
    this._config = config;
  }

  getCardSize() { return 4; }

  getGridOptions() { return { columns: "full", min_rows: 3 }; }

  set hass(hass) {
    const keys = ["grid_import", "grid_export", "solar"].map((k) => this._config[k]);
    const states = keys.map((e) => (e ? hass.states[e] : undefined));
    const sig = states.map((s) => (s ? s.state : "")).join("|");
    if (sig === this._sig) return;
    this._sig = sig;
    this._render(states.map(kw));
  }

  _render([grid, exp, solar]) {
    if (!this._card) {
      this._card = document.createElement("ha-card");
      this.appendChild(this._card);
    }
    grid = grid || 0;
    exp = exp || 0;
    // Without an inverter: only the feed-in is known of the panels, a lower bound.
    const guess = !this._config.solar && exp > 0;
    const hasSolar = !!this._config.solar || guess;
    solar = guess ? exp : hasSolar ? solar || 0 : 0;
    const toGrid = Math.min(exp, solar);
    const solarHome = Math.max(solar - toGrid, 0);
    const home = grid + solarHome;

    const left = [{ id: "grid", name: "Net", value: grid, color: "var(--energy-grid-consumption-color, #488fc2)" }];
    if (hasSolar) left.push({ id: "solar", name: "Zonnepanelen", value: solar, min: guess, color: "var(--energy-solar-color, #ff9800)" });
    const right = [{ id: "home", name: "Woning", value: home, min: guess, color: "var(--primary-color, #03a9f4)" }];
    if (hasSolar) right.push({ id: "return", name: "Teruglevering", value: toGrid, color: "var(--energy-grid-return-color, #8353d1)" });
    const links = [{ from: "grid", to: "home", value: grid }];
    if (hasSolar) {
      links.push({ from: "solar", to: "home", value: solarHome });
      links.push({ from: "solar", to: "return", value: toGrid });
    }

    const total = Math.max(left.reduce((a, n) => a + n.value, 0), right.reduce((a, n) => a + n.value, 0));
    const avail = H - TOP - GAP * (Math.max(left.length, right.length) - 1) - 4;
    const scale = total > 0 ? avail / total : 0;
    const size = (v) => (v > 0 ? Math.max(v * scale, 2) : 0);
    const place = (nodes) => {
      let y = TOP;
      for (const n of nodes) { n.y = y; n.h = size(n.value); n.used = 0; y += n.h + GAP; }
    };
    place(left);
    place(right);
    const byId = Object.fromEntries([...left, ...right].map((n) => [n.id, n]));

    let svg = "";
    for (const l of links) {
      const h = size(l.value);
      if (!h) continue;
      const a = byId[l.from], b = byId[l.to];
      const y0 = a.y + a.used, y1 = b.y + b.used;
      a.used += h; b.used += h;
      const x0 = NODE, x1 = W - NODE, mx = W / 2;
      svg += `<path d="M${x0},${y0} C${mx},${y0} ${mx},${y1} ${x1},${y1} L${x1},${y1 + h} ` +
             `C${mx},${y1 + h} ${mx},${y0 + h} ${x0},${y0 + h} Z" fill="${a.color}" opacity="0.35"/>`;
    }
    const node = (n, x, anchor, tx) => {
      if (!n.h && n.id !== "grid" && n.id !== "home") return "";
      return `<rect x="${x}" y="${n.y}" width="${NODE}" height="${Math.max(n.h, 2)}" fill="${n.color}"/>` +
             `<text x="${tx}" y="${n.y - LABEL}" text-anchor="${anchor}">${n.name} ${n.min ? "≥ " : ""}${fmt(n.value)}</text>`;
    };
    for (const n of left) svg += node(n, 0, "start", 0);
    for (const n of right) svg += node(n, W - NODE, "end", W);

    const title = this._config.title ? `<h1 class="card-header">${this._config.title}</h1>` : "";
    this._card.innerHTML = `${title}<div style="padding: ${title ? 0 : 16}px 16px 16px">` +
      `<svg viewBox="0 0 ${W} ${H}" width="100%" style="display:block;overflow:visible;` +
      `font: 12px var(--ha-font-family-body, sans-serif); fill: var(--primary-text-color)">${svg}</svg>` +
      (guess ? `<div style="font-size: 12px; color: var(--secondary-text-color); margin-top: 8px">` +
               `Omvormer nog niet gekoppeld: van de zonnepanelen is alleen de teruglevering bekend.</div>` : "") +
      `</div>`;
  }
}

// A state in kW; W is converted. Unknown or unavailable counts as nothing.
function kw(s) {
  if (!s) return undefined;
  const v = parseFloat(s.state);
  if (isNaN(v)) return 0;
  return (s.attributes.unit_of_measurement || "").toLowerCase() === "w" ? v / 1000 : v;
}

function fmt(v) {
  return v.toFixed(2).replace(".", ",") + " kW";
}

if (!customElements.get("martha-power-flow")) {
  customElements.define("martha-power-flow", MarthaPowerFlow);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: "martha-power-flow", name: "Stroom nu (martha)" });
}
