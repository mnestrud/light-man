# Light Man — Web Control Panel (plan)

**Status:** planning (added 2026-06-10; revised to the custom-panel architecture). Tracked in
[`POST-LAUNCH.md`](POST-LAUNCH.md).

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

## App layout (tabs, now free-form)

Same information as before, but laid out as a real app instead of cards:

1. **Overview** — live house state: per-source current target (brightness/color/mode) updating in real
   time, push on/off, sleep state + ramp progress, active holds with countdowns, big action buttons.
2. **Profiles** — per-source editor: sliders + numeric inputs for min/max brightness, warm/cool ct
   endpoints, sat, dusk/night floors; a daytime base **color picker**; `day_window` start/end; and a live
   **elevation→target curve** (reuse `scripts/adaptive_sim.py`'s math in JS or have the backend compute it).
3. **Sleep** — global toggle, ramp-in/out, per-source sleep targets, with a preview of the ramp.
4. **Occupancy** — per-zone enable + timings; a **live sweep visualizer** (hallway segments lighting in
   order on a floorplan-ish strip) fed by the occupancy WS stream; note/link for Z2M-side mmwave tuning.
5. **Activity / log** — a live tail of Light Man's MQTT `/set` publishes + dedup skips + issues (the Z2M
   "I can see what it's doing" view), straight from the coordinator.

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

## Implementation phases

1. **Backend API.** Add `http.py` (HomeAssistantView config read/write + actions) and a websocket command
   module (live engine/diagnostics/occupancy/publish-log streams). Wire to the coordinator + Store. Tests.
2. **Panel registration + static serving.** Register the sidebar panel and serve a placeholder bundle;
   confirm it loads in the sidebar with the `hass` socket available.
3. **Frontend app.** Build the SPA + the five tabs against the API. Start read-only (Overview/Activity),
   then add the editors (Profiles/Sleep/Occupancy) with write-back + reset-to-seed.
4. **Polish.** Curve/visualizer, auth/admin gating (`require_admin`), mobile layout.

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
