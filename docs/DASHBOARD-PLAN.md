# Light Man — Web Control Panel (plan)

**Status:** planning (added 2026-06-10; custom-panel architecture; **data model reconciled 2026-06-11**).
Tracked in [`POST-LAUNCH.md`](POST-LAUNCH.md). The topology model the panel sits on — curve library,
first-class rooms, dissolved source, two-layer occupancy, inline sweeps — is specified in
[`reference/data-model.md`](reference/data-model.md); this doc covers the panel itself (delivery, IA, build).

## Goal

A self-hosted **HTML web app in the HA left sidebar** — like Zigbee2MQTT, Node-RED, and ESPHome present
their own full UIs — not a Lovelace dashboard. The integration serves its own single-page app and a
backend API; the app gives us full layout freedom (tabs, live curves, per-room grids, an occupancy
visualizer, an MQTT publish log) without being constrained to HA's card set. This is the primary way to
operate and configure Light Man, replacing the static `light_man_config.json` seed for day-to-day tuning
and avoiding a config/options flow.

## Architecture: custom panel + backend API (the Z2M/Node-RED model, integration-style)

Z2M and Node-RED are **add-ons** — separate web servers embedded via Supervisor **ingress**. Light Man is
a **custom integration**, so the equivalent is:

1. **Sidebar panel.** The integration registers a custom panel so "Light Man" appears in the left sidebar
   and opens our app full-screen. Two delivery options:
   - **`panel_custom` (web component)** — register a JS module as the panel; HA injects the `hass`
     connection object, so the app can call HA's websocket API directly *and* our own commands. Native,
     no separate auth.
   - **`embed_iframe` panel → our served page** — register an iframe panel pointing at a static page the
     integration serves; the page runs as a standalone SPA and talks to our HTTP/WS API. Closest to the
     Z2M "it's just our web UI in a frame" feel; we own the whole document.
   Lean toward `panel_custom` with a web-component shell that mounts the SPA — we get HA auth + the `hass`
   socket for free, and still render a fully custom document inside the panel.
2. **Serve the assets.** The integration serves the built `index.html` + JS/CSS bundle from its package
   dir via the static-path API, and registers the panel pointing at it.
3. **Backend API (the interesting part).** The integration registers endpoints the app reads/writes:
   - **HTTP views** (`HomeAssistantView`: `GET/POST` JSON) for config read/write — get/set the per-source
     `SourceProfile`, sleep, and `OccupancyZone` config in the Store; reset-to-seed; trigger `force_push`
     / `clear_holds` / `release_hold`.
   - **Websocket commands** (registered command handlers) for **live streaming** — push the engine
     snapshot, the diagnostics dict, occupancy edges, and the MQTT publish log to the app in real time
     (the panel subscribes; no polling). This is what makes it feel like Z2M's live view.
   - The app may also subscribe to MQTT indirectly via these WS commands (the coordinator already sees the
     traffic) rather than opening its own broker connection from the browser.

```
 Browser (sidebar panel SPA)
   │  HA auth (panel_custom gives us the hass socket)
   ├── WS: subscribe → engine snapshot / diagnostics / occupancy / publish-log   (live)
   └── HTTP: GET/POST /api/light_man/config, /reset, /actions                     (read/write Store)
                                   │
 Integration (coordinator + new http.py / websocket_api.py + static panel bundle)
                                   │
                         Store config  +  MQTT (Z2M)
```

## App layout — room-centric IA

The model is **room-centric** (the human mental model); curves and occupancy are sibling views; the linter
is cross-cutting. Built on the four overlay graphs and reconciled schema in
[`reference/data-model.md`](reference/data-model.md). Tabs:

1. **Overview** — live house state: per-group current target (brightness/color/mode) updating in real time,
   push on/off, sleep state + ramp progress, active holds with countdowns, big action buttons.
2. **Rooms (home / physical)** — the primary, task-oriented surface. A room shows its **lights** (each with
   its source-group + assigned curve), its **switches**, and its **sensors**. Edit relationships *inline*:
   set a light/room's `curve_ref`; see (read-only) what each switch is **bound** to in Z2M; see the
   cross-room edges ("these sensors also feed the Stairwell zone"). "Tune this room" lives here.
3. **Curves (library + editor)** — named, reusable curves. Each shows the **elevation→target
   visualization**: a brightness line across the solar arc, a color strip (CT gradient or flat RGB swatch
   **per regime**), a **summer/equinox/winter** selector (the seasonal swing is real — `REF=71.5°` is
   fixed), and a sleep-ramp preview. Create / duplicate-and-tweak / delete; a **"Used by"** backref shows
   blast radius. **Day color** and **Sleep color** are each a mode toggle (CT ramp ↔ fixed RGB) — exactly
   what separates `hallway_up` (fixed sky-blue→orange) from `hallway_down` (CT day→fixed purple). Curve math
   reused from `custom_components/light_man/adaptive.py` / `reference/adaptive-algorithm.md` (port to JS or
   compute server-side).
4. **Sleep** — global toggle, ramp-in/out, per-curve sleep targets, with a preview of the ramp.
5. **Occupancy (zones + sweep builder)** — per zone: its sensors (room-owned; cross-room ones flagged),
   off-targets + all-clear rule. Per (zone, sensor): a **timeline sweep builder** — ordered stages, per-stage
   delay, drag room **fixtures/switches** into stages — with a live replay and **duplicate / mirror**
   actions. Sweeps reference room fixtures by id (resolving to set_topic + curve), so they can't drift from
   the room's lights. Note/link for Z2M-side mmwave tuning.
6. **Activity / log** — a live tail of Light Man's MQTT `/set` publishes + dedup skips + issues (the Z2M
   "I can see what it's doing" view), straight from the coordinator.

### Linter (cross-cutting "smart alerts")

Surfaced wherever relevant (room view, curve "Used by", zone view) and as a consolidated list; later
promotable to HA repair issues. The full check list is in
[`reference/data-model.md`](reference/data-model.md#config-linter-the-smart-alerts) — dangling `curve_ref`,
orphan sensors, sweep fixtures owned by no room, missing `hue_native_control`, switch bound to the wrong
group, bulb in a per-room but not the consolidated group, etc.

## Build pipeline

- A small frontend in `frontend/` (Lit or Preact or vanilla + Vite). `npm run build` emits a hashed
  bundle into `custom_components/light_man/panel/`, which ships in the integration (and via robocopy to
  live). Keep the bundle dependency-light so HACS install stays a copy-paste.
- Add the build step to CI (lint/build the panel) without making it block the Python gate.

## Trade-offs vs. the Lovelace + config-entities approach (the one this replaces)

| | Custom HTML panel (this) | Lovelace + config entities |
|---|---|---|
| Flexibility | Full — any layout, live graphs, logs, visualizers | Limited to HA cards |
| Code/maintenance | A real frontend + build + HTTP/WS API layer | Just expose entities; no frontend |
| Auth/state | Reuse HA auth via `panel_custom`; own API for the rest | All free via HA |
| HA-native niceties | Lose entity history/recorder, card ecosystem, voice | Keep them |
| Feels like | Z2M / Node-RED | HA settings |

Chosen: **custom HTML panel** for the flexibility (explicit user call, 2026-06-10). Accept the extra
frontend + API surface. We can still expose a *few* core toggles (`push_enable`, `sleep`) as HA entities
in parallel so they remain scriptable/voice-controllable.

## Sequencing principle — design first, API last

**Hard rule (user, 2026-06-10): we design the panel's layout and configuration approach first, confirm it,
and only THEN design the API.** The API shape is *derived from* what the confirmed UI actually needs — never
the other way round. Do not write or design any HTTP/WS endpoint until the design approach is signed off.
The "Architecture" section above is the *delivery mechanism* (how a panel gets into the sidebar at all),
not a commitment to specific endpoints; treat its API bullets as candidates to be settled in Phase 2.

## Implementation phases

1. **Design the panel (UX + configuration approach).** No code. **Largely done** — the configuration model
   (what's editable, at what granularity, how it maps onto the Store) is decided and specified in
   [`reference/data-model.md`](reference/data-model.md); the IA + editable/read-only surfaces are the
   "App layout" section above. Remaining: per-tab wireframes/mockups, validation/limits, apply semantics
   (live vs. save), and reset-to-seed. **Gate: confirm the design approach (this doc + data-model.md) before
   proceeding.** Nothing past this phase starts until sign-off.
1b. **Schema + migration (foundational).** Land the reconciled model in `models.py` (`curves{}` library,
   first-class `Room`, light `curve_ref`+group tag, `OccupancyZone.sensors` as references, sweep `lights` as
   room-fixture ids), with a **behaviour-preserving** old→new loader migration in `config_loader.py` and the
   rewritten `light_man_config.json`. A test asserts the no-hold/no-override push plan is byte-identical to
   today (4 consolidated floods). The coordinator's addressing generalizes for curve-divergent members
   (`push.plan_publishes`) — see the addressing implication in data-model.md. This is independent of the
   panel and can ship as a normal release ahead of it.
2. **Design the API (only after sign-off).** Derive the minimal HTTP views + websocket commands from the
   confirmed UI: config read/write endpoints, action endpoints, and the live streams the design requires —
   shaped to the screens, not invented up front. (Confirm current `panel_custom` / static-path /
   WS-command API signatures with the `ha-dev` agent here.)
3. **Build the backend.** Implement the designed API (`http.py` views + websocket command module) wired to
   the coordinator + Store. Tests, 100% cov.
4. **Panel registration + static serving.** Register the sidebar panel; serve a placeholder bundle; confirm
   it loads with the `hass` socket available.
5. **Build the frontend.** The SPA + tabs against the API — read-only surfaces first, then the editors
   (room relationships, curve library, sweep builder) with write-back + reset-to-seed.
6. **Polish.** Curve/visualizer, the live linter, auth/admin gating (`require_admin`), mobile layout.

## Open decisions (resolve at build time)

- **`panel_custom` web component vs. `embed_iframe`** — confirm which gives the cleanest "own the whole
  document" feel while keeping HA auth. (Verify current panel/static-path/WS-command API signatures with
  the `ha-dev` agent before coding — these HA frontend APIs shift between releases.)
- **Frontend stack** — Lit (HA-aligned) vs. Preact/vanilla; keep the bundle small for HACS.
- **Config source of truth** — the panel writes the Store directly via the API; the bundled JSON stays as
  seed + reset baseline. Decide on optimistic-concurrency / last-writer-wins for simultaneous edits.
- **Live streams** — WS-command push vs. SSE; and whether the publish-log tail comes from the coordinator
  or a thin MQTT subscription.
- **Keep a minimal entity surface** (`push_enable`, `sleep`, maybe `force_push` button) for
  scripting/voice even though the panel is the main UI.
