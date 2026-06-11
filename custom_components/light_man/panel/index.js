// Light Man — sidebar control panel (read-only, Phase 2).
//
// Dependency-free web component (no build step, HACS copy-paste safe). HA injects
// `hass`; we read the stored topology via the `light_man/config` websocket command
// and stream live state via `light_man/subscribe`. Nothing here writes — every
// surface is a view. The adaptive-curve visualizer mirrors adaptive.py's math.

const REF_ELEVATION_DEG = 71.5; // summer solar-noon reference (fixed)
const TWILIGHT_BAND_DEG = 18.0;
const PERCEPTUAL_GAMMA = 2.2;
const DEFAULT_SAT = 0.5;
const TABS = ["Overview", "Rooms", "Curves", "Occupancy", "Activity"];

const clamp = (v, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, v));

function perceptualLerp(lo, hi, t) {
  const g = PERCEPTUAL_GAMMA;
  const a = Math.pow(lo, 1 / g);
  const b = Math.pow(hi, 1 / g);
  return Math.pow(a + (b - a) * clamp(t), g);
}

// Brightness % across the elevation regimes (daytime / twilight / night).
function curveBrightness(e, curve) {
  const minBr = curve.min_br ?? 30;
  const maxBr = curve.max_br ?? 90;
  const nightFloor = curve.night_floor_br ?? minBr;
  if (e >= 0) {
    const denom = (curve.sat ?? DEFAULT_SAT) * REF_ELEVATION_DEG;
    return perceptualLerp(minBr, maxBr, denom > 0 ? clamp(e / denom) : 1);
  }
  if (e >= -TWILIGHT_BAND_DEG) {
    return perceptualLerp(minBr, nightFloor, clamp(-e / TWILIGHT_BAND_DEG));
  }
  return nightFloor;
}

// Approximate Kelvin -> CSS color for the color strip (warm amber -> cool blue).
function kelvinToCss(k) {
  const t = clamp((k - 2000) / (6500 - 2000));
  const r = Math.round(255);
  const g = Math.round(150 + 90 * t);
  const b = Math.round(70 + 175 * t);
  return `rgb(${r},${Math.min(g, 255)},${Math.min(b, 255)})`;
}

const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

const short = (topic) =>
  esc(String(topic ?? "").replace(/^zigbee2mqtt\//, "").replace(/\/set$/, ""));

class LightManPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._inited = false;
    this._config = null; // stored topology + switch_map
    this._state = null; // live panel state
    this._tab = "Overview";
    this._curve = null; // selected curve name
    this._zone = null; // selected zone id
    this._unsub = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._inited && hass) {
      this._inited = true;
      this._connect();
    }
  }

  set panel(_p) {}
  set narrow(_n) {}
  set route(_r) {}

  async _connect() {
    this._renderShell();
    try {
      this._config = await this._hass.callWS({ type: "light_man/config" });
    } catch (err) {
      this._fatal(err);
      return;
    }
    try {
      this._unsub = await this._hass.connection.subscribeMessage(
        (state) => {
          this._state = state;
          this._renderContent();
        },
        { type: "light_man/subscribe" },
      );
    } catch (err) {
      this._fatal(err);
      return;
    }
    this._renderContent();
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
  }

  _fatal(err) {
    const body = this.shadowRoot.getElementById("body");
    if (body) {
      body.innerHTML = `<div class="empty">Failed to load Light Man: ${esc(
        err && err.message ? err.message : err,
      )}</div>`;
    }
  }

  // --- shell + tab routing --------------------------------------------------

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="wrap">
        <header>
          <div class="title">☀ Light Man</div>
          <nav id="tabs"></nav>
        </header>
        <div id="body"><div class="empty">Loading…</div></div>
      </div>`;
    const tabs = this.shadowRoot.getElementById("tabs");
    tabs.innerHTML = TABS.map(
      (t) => `<button data-tab="${t}">${t}</button>`,
    ).join("");
    tabs.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._renderContent();
      }),
    );
  }

  _renderContent() {
    if (!this.shadowRoot.getElementById("body")) this._renderShell();
    this.shadowRoot
      .querySelectorAll("#tabs button")
      .forEach((b) => b.classList.toggle("on", b.dataset.tab === this._tab));
    const body = this.shadowRoot.getElementById("body");
    const cfg = (this._config && this._config.config) || {};
    const st = this._state || {};
    const views = {
      Overview: () => this._overview(cfg, st),
      Rooms: () => this._rooms(cfg, st),
      Curves: () => this._curves(cfg, st),
      Occupancy: () => this._occupancy(cfg, st),
      Activity: () => this._activity(st),
    };
    body.innerHTML = (views[this._tab] || views.Overview)();
    this._wireContent(cfg);
  }

  _wireContent(cfg) {
    this.shadowRoot.querySelectorAll("[data-curve]").forEach((el) =>
      el.addEventListener("click", () => {
        this._curve = el.dataset.curve;
        this._renderContent();
      }),
    );
    this.shadowRoot.querySelectorAll("[data-zone]").forEach((el) =>
      el.addEventListener("click", () => {
        this._zone = el.dataset.zone;
        this._renderContent();
      }),
    );
  }

  // --- Overview -------------------------------------------------------------

  _overview(cfg, st) {
    const sleep = st.sleep || {};
    const issues = st.issues || [];
    const engine = st.engine || {};
    const held = st.held || {};
    const sources = cfg.sources || {};
    const tiles = Object.keys(sources)
      .map((key) => {
        const e = engine[key] || {};
        const mode = e.color_mode || "—";
        const color =
          mode === "rgb"
            ? e.rgb_color
              ? `rgb(${e.rgb_color.join(",")})`
              : "—"
            : e.color_temp_kelvin
              ? `${e.color_temp_kelvin}K`
              : "—";
        const swatch =
          mode === "rgb" && e.rgb_color
            ? `rgb(${e.rgb_color.join(",")})`
            : e.color_temp_kelvin
              ? kelvinToCss(e.color_temp_kelvin)
              : "#444";
        return `<div class="tile">
          <div class="tile-h"><span class="dot" style="background:${swatch}"></span>${esc(key)}</div>
          <div class="big">${e.brightness_pct != null ? Math.round(e.brightness_pct) + "%" : "—"}</div>
          <div class="sub">${esc(color)} · ${esc(mode)}</div>
        </div>`;
      })
      .join("");
    const holdRows = Object.entries(held)
      .map(
        ([ref, h]) =>
          `<tr><td>${esc(ref)}</td><td><span class="pill">${esc(h.mode)}</span></td><td>${esc(h.expires_at || "")}</td></tr>`,
      )
      .join("");
    return `
      <div class="bar">
        ${badge("Push", st.push_enabled ? "ON" : "OFF", st.push_enabled ? "good" : "off")}
        ${badge("Sleep", sleep.on ? `ON ${Math.round((sleep.s || 0) * 100)}%` : "OFF", sleep.on ? "warn" : "off")}
        ${badge("MQTT", st.mqtt_available ? "ok" : "down", st.mqtt_available ? "good" : "bad")}
        ${badge("Alerts", String(issues.length), issues.length ? "bad" : "good")}
      </div>
      <h2>Live targets (source groups)</h2>
      <div class="tiles">${tiles || empty("no source groups")}</div>
      <h2>Active holds (${Object.keys(held).length})</h2>
      ${
        holdRows
          ? `<table><thead><tr><th>Light group</th><th>Mode</th><th>Clears</th></tr></thead><tbody>${holdRows}</tbody></table>`
          : empty("no holds — everything is adaptive")
      }
      ${issues.length ? `<h2>Config alerts</h2><ul class="alerts">${issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}`;
  }

  // --- Rooms ----------------------------------------------------------------

  _rooms(cfg, st) {
    const rooms = cfg.rooms || {};
    const sources = cfg.sources || {};
    const held = st.held || {};
    const cards = Object.entries(rooms)
      .map(([roomId, room]) => {
        const lights = (room.lights || [])
          .map((l) => {
            const ref = `${roomId}.${l.id}`;
            const src = sources[l.source] || {};
            const curve = src.curve_ref || "—";
            const flood = src.consolidated_topic;
            const isHeld = held[ref];
            return `<tr${isHeld ? ' class="held"' : ""}>
              <td>${esc(l.id)}${isHeld ? ` <span class="pill">${esc(isHeld.mode)}</span>` : ""}</td>
              <td class="mono" title="${esc(l.set_topic)}">${short(l.set_topic)}</td>
              <td>${esc(l.source)} <span class="muted mono" title="${esc(flood)}">${short(flood)}</span></td>
              <td><span class="curve-chip">${esc(curve)}</span></td>
            </tr>`;
          })
          .join("");
        const lightHead =
          "<thead><tr><th>Light</th><th>Hold group (Zigbee)</th>" +
          "<th>Source · flood group</th><th>Curve</th></tr></thead>";
        const switches = (room.switches || [])
          .map(
            (s) =>
              `<tr><td class="mono">${short(s.topic)}</td><td>governs → ${esc(s.governs)}</td></tr>`,
          )
          .join("");
        const sensors = Object.entries(room.sensors || {})
          .map(
            ([name, sn]) =>
              `<tr><td>${esc(name)}</td><td class="mono">${short(sn.topic)}</td><td>${esc(sn.occupancy_key || "occupancy")}</td></tr>`,
          )
          .join("");
        return `<section class="card">
          <h3>${esc(room.name || roomId)} <span class="muted">${roomId}</span></h3>
          <div class="sect">Lights <span class="muted">— hold group = the Zigbee group commanded when this light is held</span></div>
          <table>${lightHead}<tbody>${lights || rowEmpty(4)}</tbody></table>
          ${switches ? `<div class="sect">Switches</div><table><tbody>${switches}</tbody></table>` : ""}
          ${sensors ? `<div class="sect">Sensors</div><table><tbody>${sensors}</tbody></table>` : ""}
        </section>`;
      })
      .join("");
    return cards || empty("no rooms configured");
  }

  // --- Curves ---------------------------------------------------------------

  _curves(cfg) {
    const curves = cfg.curves || {};
    const names = Object.keys(curves);
    if (!names.length) return empty("no curves configured");
    if (!this._curve || !curves[this._curve]) this._curve = names[0];
    const sources = cfg.sources || {};
    const list = names
      .map(
        (n) =>
          `<button data-curve="${esc(n)}" class="${n === this._curve ? "on" : ""}">${esc(n)}</button>`,
      )
      .join("");
    const curve = curves[this._curve];
    const usedBy = Object.entries(sources)
      .filter(([, s]) => s.curve_ref === this._curve)
      .map(([k]) => k);
    return `<div class="split">
      <div class="lib">${list}</div>
      <div class="editor">
        <h3>${esc(this._curve)}</h3>
        <div class="muted">Used by: ${usedBy.length ? usedBy.map(esc).join(", ") : "— (unassigned)"}</div>
        ${curveSvg(curve)}
        ${curveFields(curve)}
      </div>
    </div>`;
  }

  // --- Occupancy ------------------------------------------------------------

  _occupancy(cfg, st) {
    const zones = cfg.occupancy_zones || {};
    const ids = Object.keys(zones);
    if (!ids.length) return empty("no occupancy zones configured");
    if (!this._zone || !zones[this._zone]) this._zone = ids[0];
    const occ = st.occupancy || {};
    const list = ids
      .map(
        (z) =>
          `<button data-zone="${esc(z)}" class="${z === this._zone ? "on" : ""}">${esc(z)}${occ[z] ? " ●" : ""}</button>`,
      )
      .join("");
    const zone = zones[this._zone];
    const sensors = Object.entries(zone.sensors || {})
      .map(([ref, binding]) => {
        const stages = (binding.sweep || [])
          .map((stg) => {
            const lights = (stg.lights || []).map((r) => `<span class="fx">${esc(r)}</span>`).join("");
            const delay = stg.delay_s ? `<span class="delay">+${stg.delay_s}s</span>` : "";
            return `<div class="stage">${delay}${lights}</div>`;
          })
          .join('<span class="arrow">→</span>');
        return `<div class="sensor">
          <div class="sensor-h">${esc(ref)}</div>
          <div class="sweep">${stages || empty("no sweep")}</div>
        </div>`;
      })
      .join("");
    const off = (zone.off_lights || []).map(short).join(", ") || "—";
    return `<div class="split">
      <div class="lib">${list}</div>
      <div class="editor">
        <h3>${esc(this._zone)} zone</h3>
        <div class="muted">All-clear off → ${off} · transition ${zone.off_transition_s ?? 1.5}s</div>
        ${sensors}
      </div>
    </div>`;
  }

  // --- Activity -------------------------------------------------------------

  _activity(st) {
    const pubs = Object.entries(st.last_publish || {});
    const inv = Object.entries(st.inovelli || {});
    const rows = [
      ...pubs.map(([t, p]) => ({ kind: "set", topic: t, payload: p })),
      ...inv.map(([t, p]) => ({ kind: "switch", topic: t, payload: p })),
    ]
      .map(
        (r) =>
          `<tr><td><span class="pill">${r.kind}</span></td><td class="mono">${short(r.topic)}</td><td class="mono small">${esc(JSON.stringify(r.payload))}</td></tr>`,
      )
      .join("");
    return `<div class="muted">Latest publish per topic (live snapshot · ${st.dedup_skips || 0} dedup skips)</div>
      ${rows ? `<table><thead><tr><th>kind</th><th>topic</th><th>payload</th></tr></thead><tbody>${rows}</tbody></table>` : empty("nothing published yet")}`;
  }
}

// --- shared render helpers --------------------------------------------------

const badge = (label, value, cls) =>
  `<span class="badge ${cls}"><b>${esc(label)}</b> ${esc(value)}</span>`;
const empty = (msg) => `<div class="empty">${esc(msg)}</div>`;
const rowEmpty = (cols) => `<tr><td colspan="${cols}" class="muted">none</td></tr>`;

function curveFields(c) {
  const rows = [
    ["Brightness", `${c.min_br ?? "—"}% → ${c.max_br ?? "—"}% · sat ${c.sat ?? DEFAULT_SAT} · night floor ${c.night_floor_br ?? "—"}%`],
    [
      "Day color",
      c.base_color_mode === "rgb"
        ? `fixed rgb(${(c.base_rgb || []).join(",")})`
        : `CT ramp ${c.min_ct ?? "—"}K → ${c.max_ct ?? "—"}K (dusk ${c.dusk_floor_ct ?? "—"}K)`,
    ],
    [
      "Sleep",
      `${(c.sleep || {}).br ?? "—"}% · ${
        (c.sleep || {}).color_mode === "rgb"
          ? `rgb(${((c.sleep || {}).rgb || []).join(",")})`
          : `${(c.sleep || {}).ct ?? "—"}K`
      }`,
    ],
    [
      "Day window",
      c.day_window && c.day_window.enabled
        ? `${c.day_window.start}–${c.day_window.end}`
        : "off",
    ],
  ];
  return `<table class="fields"><tbody>${rows
    .map(([k, v]) => `<tr><td class="muted">${esc(k)}</td><td>${esc(v)}</td></tr>`)
    .join("")}</tbody></table>`;
}

function curveSvg(curve) {
  const W = 420;
  const H = 150;
  const pad = 26;
  const e0 = -TWILIGHT_BAND_DEG;
  const e1 = REF_ELEVATION_DEG;
  const x = (e) => pad + ((e - e0) / (e1 - e0)) * (W - 2 * pad);
  const y = (br) => H - pad - (clamp(br / 100) * (H - 2 * pad));
  const pts = [];
  for (let i = 0; i <= 60; i++) {
    const e = e0 + ((e1 - e0) * i) / 60;
    pts.push(`${x(e).toFixed(1)},${y(curveBrightness(e, curve)).toFixed(1)}`);
  }
  // color strip beneath the axis
  let strip;
  if (curve.base_color_mode === "rgb" && curve.base_rgb) {
    strip = `<rect x="${pad}" y="${H - pad + 4}" width="${W - 2 * pad}" height="8" fill="rgb(${curve.base_rgb.join(",")})" />`;
  } else {
    const stops = [];
    for (let i = 0; i <= 8; i++) {
      const k = (curve.min_ct ?? 2700) + (((curve.max_ct ?? 6500) - (curve.min_ct ?? 2700)) * i) / 8;
      stops.push(`<stop offset="${(i / 8) * 100}%" stop-color="${kelvinToCss(k)}" />`);
    }
    strip = `<defs><linearGradient id="ct">${stops.join("")}</linearGradient></defs>
      <rect x="${pad}" y="${H - pad + 4}" width="${W - 2 * pad}" height="8" fill="url(#ct)" />`;
  }
  const zeroX = x(0);
  return `<svg viewBox="0 0 ${W} ${H}" class="viz">
    <line x1="${pad}" y1="${H - pad}" x2="${W - pad}" y2="${H - pad}" class="axis" />
    <line x1="${zeroX}" y1="${pad - 6}" x2="${zeroX}" y2="${H - pad}" class="axis dash" />
    <polyline points="${pts.join(" ")}" class="curve" />
    ${strip}
    <text x="${pad}" y="${H - 4}" class="lbl">-18°</text>
    <text x="${zeroX - 6}" y="${H - 4}" class="lbl">0°</text>
    <text x="${W - pad - 18}" y="${H - 4}" class="lbl">${e1}°</text>
  </svg>`;
}

const STYLES = `
:host { display:block; --bg:var(--primary-background-color,#111); --card:var(--card-background-color,#1c1c1c);
  --fg:var(--primary-text-color,#e8e8e8); --mut:var(--secondary-text-color,#9aa0a6);
  --acc:var(--primary-color,#ffb300); --line:var(--divider-color,#333); color:var(--fg);
  font-family:-apple-system,Roboto,Segoe UI,sans-serif; }
.wrap { padding:0 0 40px; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:12px 20px; z-index:2; }
.title { font-size:20px; font-weight:600; margin-bottom:8px; }
nav { display:flex; gap:4px; flex-wrap:wrap; }
nav button { background:transparent; color:var(--mut); border:none; padding:8px 14px; border-radius:8px 8px 0 0;
  cursor:pointer; font-size:14px; }
nav button.on { color:var(--fg); background:var(--card); border-bottom:2px solid var(--acc); }
#body { padding:18px 20px; }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.05em; color:var(--mut); margin:22px 0 10px; }
h3 { margin:0 0 4px; font-size:16px; }
.muted, .muted * { color:var(--mut); font-weight:400; font-size:13px; }
.empty { color:var(--mut); padding:20px; text-align:center; font-style:italic; }
.bar { display:flex; gap:10px; flex-wrap:wrap; }
.badge { background:var(--card); border:1px solid var(--line); border-radius:20px; padding:6px 12px; font-size:13px; }
.badge b { color:var(--mut); font-weight:500; margin-right:4px; }
.badge.good { border-color:#2e7d32; } .badge.bad { border-color:#c62828; } .badge.warn { border-color:#f9a825; }
.badge.off { opacity:.6; }
.tiles { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:12px; }
.tile { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; }
.tile-h { display:flex; align-items:center; gap:8px; font-weight:600; }
.tile .big { font-size:26px; margin:6px 0 2px; }
.tile .sub { color:var(--mut); font-size:12px; }
.dot { width:12px; height:12px; border-radius:50%; display:inline-block; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; color:var(--mut); font-weight:500; border-bottom:1px solid var(--line); padding:6px 8px; }
td { padding:6px 8px; border-bottom:1px solid var(--line); }
tr.held td { background:rgba(255,179,0,.08); }
.mono { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px; }
.small { color:var(--mut); }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin-bottom:14px; }
.card .muted { font-size:12px; margin-left:6px; }
.sect { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.05em; margin:12px 0 4px; }
.pill { background:var(--acc); color:#000; border-radius:6px; padding:1px 7px; font-size:11px; font-weight:600; }
.curve-chip { background:#2a2a2a; border:1px solid var(--line); border-radius:6px; padding:2px 8px; font-size:12px; }
.alerts { margin:0; padding-left:18px; } .alerts li { color:#ef9a9a; font-size:13px; margin:4px 0; }
.split { display:grid; grid-template-columns:180px 1fr; gap:18px; }
.lib { display:flex; flex-direction:column; gap:4px; }
.lib button { text-align:left; background:var(--card); border:1px solid var(--line); color:var(--fg);
  padding:9px 12px; border-radius:8px; cursor:pointer; font-size:14px; }
.lib button.on { border-color:var(--acc); color:var(--acc); }
.editor { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
.viz { width:100%; max-width:440px; margin:10px 0; }
.viz .axis { stroke:var(--line); stroke-width:1; } .viz .dash { stroke-dasharray:3 3; }
.viz .curve { fill:none; stroke:var(--acc); stroke-width:2; }
.viz .lbl { fill:var(--mut); font-size:9px; }
.fields td { border:none; padding:4px 8px; } .fields td:first-child { width:110px; }
.sensor { margin:14px 0; } .sensor-h { font-weight:600; margin-bottom:6px; }
.sweep { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.stage { background:#262626; border:1px solid var(--line); border-radius:8px; padding:6px 8px; display:flex; flex-direction:column; gap:3px; }
.fx { font-family:ui-monospace,monospace; font-size:11px; }
.delay { color:var(--acc); font-size:10px; }
.arrow { color:var(--mut); }
`;

customElements.define("light-man-panel", LightManPanel);
