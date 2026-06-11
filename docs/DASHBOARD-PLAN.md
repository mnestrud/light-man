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

The model is **room-centric** (the human mental model); curves and occupancy are sibling library/visual
views; the linter is cross-cutting. Built on the four overlay graphs and reconciled schema in
[`reference/data-model.md`](reference/data-model.md). **5 tabs** (Sleep dissolved into Overview + the Curve
editor — see data-model.md S3):

1. **Overview** — live house state + global controls. Per **source-group** target tiles (current
   brightness/color/mode from the engine snapshot); **push on/off**; the global **push interval**
   (`push_interval_s`, the cadence in seconds); **sleep on/off** (`switch.light_man_sleep`) with **ramp
   progress** and the global **ramp-in/ramp-out** timing; **force push** + **clear all holds**; the
   **active holds** list (which light groups, the look, countdowns, per-hold release); a **linter summary
   badge**.
2. **Rooms** — the primary, task-oriented surface ("tune this room"). One card per **physical room**
   (merged; kitchen = overhead + island groups together). **Lights** organized by their **source group**,
   each showing the group's **assigned curve** — curve assignment is **per source group** here (S1; the main
   edit on this tab; a "+ override this group's curve" affordance exists but per-light override is **not** in
   v1). **Switches** each show the **light group they govern/hold** (S2) + a read-only view of what they're
   **bound** to in Z2M. **Sensors** the room owns, cross-room edges flagged ("also feeds the Stairwell
   zone"). Inline linter chips on anything broken.
3. **Curves (library + editor)** — named, reusable curves with a **"Used by"** backref (which source groups →
   blast radius). **Viz (shipped v0.7.0, read-only):** a **time-of-day** brightness arc — rise → peak →
   **sunset wind-down** — computed *server-side by the real engine* (`coordinator.curve_previews()` samples
   today's sun path and runs `compute_target`, so the picture matches what gets published, day-window and all),
   with x-axis labels at **dawn/sunrise/noon/sunset/dusk** (today's actual times), a color strip following the
   day, and the **sleep target as a reference line**. **Day color** and **Sleep color** are each a mode toggle
   (CT ramp ↔ fixed RGB) — what separates `hallway_up` (fixed sky-blue→orange) from `hallway_down` (CT
   day→fixed purple); the **per-curve sleep target lives here** (S3).
   **Phase 3 — what the editor will make configurable (per curve):**
   - **Brightness:** `min_br`, `max_br`, `sat` (how early in the morning it saturates to full), `night_floor_br`.
   - **Day color:** mode (CT ramp ↔ fixed RGB); CT → `min_ct`/`max_ct`/`dusk_floor_ct`; RGB → `base_rgb` swatch.
   - **Sleep target:** `sleep.br` + mode (`sleep.ct` or `sleep.rgb`).
   - **Sunset wind-down / sunrise ramp rate** (the curve's own "ramp down at sunset"): the optional `day_window`
     — `enabled`, `start`/`end`, and its **`edge_transition_s`** (ramp-*up* rate into the curve at the window
     start) and **`wind_down_s`** (ramp-*down* rate to the night floor after the window end). These are the
     per-curve "ramp up / ramp down" rates the viz already draws.
   Plus create / duplicate-and-tweak / delete. (The **global sleep ramp** — `ramp_in_s`/`ramp_out_s`, the
   ease into/out of the *sleep overlay* — is edited on **Overview**, not per curve; see below.) Math reused
   from `custom_components/light_man/adaptive.py` / `reference/adaptive-algorithm.md`.
4. **Occupancy (zones + sweep builder)** — per zone: its sensors (room-owned; cross-room ones flagged),
   off-targets + all-clear rule. Per (zone, sensor): a **timeline sweep builder** — ordered stages, per-stage
   delay, drag room **fixtures/switches** into stages — with a live replay. Sweeps reference room fixtures by
   id (resolving to set_topic + curve), so they can't drift from the room's lights. **Mirror / duplicate**
   sweep actions are editor sugar — **deferred to Polish**. Note/link for Z2M-side mmwave tuning.
5. **Activity / log** — a live tail of Light Man's MQTT `/set` publishes + dedup skips + issues (the Z2M
   "I can see what it's doing" view), straight from the coordinator.

### Config menu (app-wide)

A header menu present on every tab: **Save** (= Save & Export — writes the Store + the device config file),
**Save As…** (download the current config as an external snapshot), **Load config…** (upload + validate +
swap a snapshot into the Store — for A/B testing), and **Reset** (revert to the shipped factory default,
global or scoped). See the four-artifact model in
[`reference/data-model.md`](reference/data-model.md#config-persistence--files-dashboard).

### Linter (cross-cutting "smart alerts")

Surfaced wherever relevant (room view, curve "Used by", zone view) and as a consolidated list (Overview
badge); later promotable to HA repair issues. The full check list is in
[`reference/data-model.md`](reference/data-model.md#config-linter-the-smart-alerts) — dangling `curve_ref`,
orphan sensors, sweep fixtures owned by no room, **`off_lights` resolving to no known Z2M group**, missing
`hue_native_control`, switch bound to a different light group than it governs, bulb in a per-room but not the
consolidated group, etc.

## Wireframes (Phase-1 deliverable)

Low-fidelity, to lock layout + interaction — not visual design. Grounded in the real seed (merged Kitchen,
the 4 curves, the Hallway/Stairwell occupancy). Lit + hand-rolled SVG/canvas for the viz; stage-then-Save.

### 1. Overview — live state + global controls
```
+- Light Man -------------------------------- [Rooms][Curves][Occupancy][Activity] -+
|                                                                                    |
|  Push [ ON o]  every 30s   Sleep [o OFF]  ramp 90m/30m   [ Force push ] [ Clear holds ] |
|                                                                   (!) 2 alerts >   |
|  Live targets (source groups)                                                      |
|  +--------------+ +--------------+ +--------------+ +--------------+               |
|  | Overhead     | | Accent       | | Hallway Up   | | Hallway Down |               |
|  | 90%  5200K   | | 28%  4100K   | | 90%  #sky    | | 75%  4800K   |               |
|  | adaptive     | | adaptive     | | rgb          | | color_temp   |               |
|  +--------------+ +--------------+ +--------------+ +--------------+               |
|                                                                                    |
|  Active holds (2)                                                                  |
|   - Kitchen / Overhead   night    clears 12:00am   [release]                       |
|   - Office  / Overhead   manual   clears 12:00am   [release]                       |
+------------------------------------------------------------------------------------+
```

### 2. Rooms — primary "tune this room" surface
```
+- Rooms ----------------------------------------------------------------------------+
|  [ search... ]                                                                     |
|  +- Kitchen ----------------------------------------------------------------------+|
|  | Lights                                                                         ||
|  |   Overhead (zgb_kitchen)         curve [ daylight_standard v ]  (group-wide)   ||
|  |   Island   (zgb_kitchen_island)  curve [ accent_dim v ]         (group-wide)   ||
|  | Switches                                                                       ||
|  |   Kitchen Overhead Switch  governs > Overhead  . bound: zgb_kitchen        (ok)||
|  |   Kitchen Island Switch    governs > Island    . bound: zgb_kitchen_island (ok)||
|  | Sensors  (none)                                                                ||
|  +--------------------------------------------------------------------------------+|
|  +- Hallway ---------------------------------------------------------------------+|
|  | Lights  6 fixtures / 2 groups                                                  ||
|  |   *_up   (zgb_hallway_up)    curve [ hallway_sky v ]                           ||
|  |   *_down (zgb_hallway_down)  curve [ hallway_ct_winddown v ]                   ||
|  | Switches  (none holdable)                                                      ||
|  | Sensors                                                                        ||
|  |   hall_east  ->  Hallway zone                                                  ||
|  |   hall_west  ->  Hallway zone, Stairwell zone   (cross-room)                   ||
|  +--------------------------------------------------------------------------------+|
|  (!) Office: switch bound to zgb_office but governs Overhead/zgb_office_overhead   |
+------------------------------------------------------------------------------------+
```
Note: the curve dropdown edits the **source group's** curve (S1) — "group-wide", with a Used-by warning;
per-light/room override is deferred (a disabled "+ override" affordance hints at it).

### 3. Curves — library + editor/visualizer
```
+- Curves ---------------------------------------------------------------------------+
|  Library                       |  Editor - daylight_standard                       |
|  +---------------------------+ |  Used by: Overhead group (10 rooms)               |
|  | > daylight_standard       | |  Season: (Summer) (Equinox*) (Winter)             |
|  |   accent_dim              | |  br% |                 _________                   |
|  |   hallway_sky             | |   90 |          ______/                           |
|  |   hallway_ct_winddown     | |   30 |____ ____/                                  |
|  | [+ new] [dup] [delete]    | |      +--+-----+-----+-----+--  elevation          |
|  +---------------------------+ |       -18    0    35    71                        |
|                                |  color |warm|...gradient...|cool|                  |
|                                |  Brightness  min[30] max[90] sat[0.5] nfloor[30]   |
|                                |  Day color   (CT ramp*) (fixed RGB)               |
|                                |              min_ct[2700] max_ct[6500] dusk[2200] |
|                                |  Sleep       br[30] (CT*)(RGB) ct[2700]           |
|                                |  Day window  [x] 08:00-17:00                       |
|                                |  ~ ramp preview ~   in 90m / out 30m              |
|                                |                            [ Cancel ] [ Save ]    |
+------------------------------------------------------------------------------------+
```
For an RGB curve (`hallway_sky`) the color strip is a flat swatch and the CT knobs (`min_ct/max_ct/dusk`)
grey out; Day color toggle sits on "fixed RGB" with a color picker.

### 4. Occupancy — zones + sweep builder
```
+- Occupancy ------------------------------------------------------------------------+
|  Zones                  |  Hallway zone                                            |
|  +-------------------+   |  Sensors:  hall_east (Hallway)                          |
|  | > Hallway         |   |            hall_west (Hallway)  -> also Stairwell        |
|  |   Stairwell       |   |  Off when: all sensors clear                            |
|  +-------------------+   |  Off targets: zgb_hallwayf   transition 1.5s            |
|                                                                                    |
|  Sweep - hall_east                                       [ mirror -> hall_west ]   |
|  +- stage 1 --+  +1.0s  +- stage 2 --+  +1.0s  +- stage 3 --+                      |
|  | east_up    | ------> | center_up  | ------> | west_up    |                      |
|  | east_down  |         | center_down|         | west_down  |                      |
|  +------------+         +------------+         +------------+                      |
|  [+ fixture]   drag from Hallway v                              [ > replay ]       |
+------------------------------------------------------------------------------------+
```

### 5. Activity / log
```
+- Activity -------------------------------------------------------------------------+
|  [ all v ]  [ || pause ]                                              live o       |
|  12:04:01  set   zgb_overhead_all     bri 254  ct 5200K                            |
|  12:04:01  set   zgb_accent_all       bri 71   ct 4100K                            |
|  12:04:01  skip  zgb_hallway_up       (dedup, unchanged)                           |
|  12:03:58  hold  kitchen/overhead -> night   (tap config_single)                   |
|  12:03:31  occ   Hallway hall_east occupied -> sweep x3                            |
|  (!)12:03:02 issue  sweep light 'stairwell.overhead' owned by no room              |
+------------------------------------------------------------------------------------+
```

## Build pipeline

- **v0.7.0: no build step.** `custom_components/light_man/panel/index.js` is hand-authored vanilla JS that
  ships as-is (robocopy mirrors it to live; HACS copies it). This keeps install a pure copy-paste.
- **Deferred (Polish, with the Lit migration):** a small frontend in `frontend/` (Lit + Vite); `npm run build`
  emits a hashed bundle into `custom_components/light_man/panel/`. Add the build step to CI (lint/build the
  panel) without making it block the Python gate.

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

Canonical phasing (this is the single source — `data-model.md`'s "Staged work" points here). Five phases,
each an independently shippable increment; the dependency order is schema → read → write → polish. **No API
endpoint is designed until the screen that needs it is being built** — the design-first rule is satisfied
because the UI + data model are signed off in Phase 0.

0. **Design — DONE ✓.** The reconciled data model, the room-centric IA, the panel architecture, the 5-tab
   wireframes, and the config-persistence model are all specified ([`reference/data-model.md`](reference/data-model.md)
   + the App-layout / Architecture sections above). This phase is the **sign-off gate**; nothing past it
   starts until the design is confirmed.
1. **Schema migration (foundational; normal release, no panel). ✅ DONE — `v0.6.0` (2026-06-11).** Landed the
   reconciled model in `models.py` (stored `Curve`/`SourceGroup`/`Room`/`RoomLight`/`RoomSwitch`/`RoomSensor`/
   `ZoneConfig`/`SleepConfig`/`StoredConfig`) + the `switch_map` value-shape change (M2, `base → (group,
   light_ref)`); rewrote `light_man_config.json` to the new shape and bumped `seed_version` → **8**. Key move:
   `config_loader.py` **derives the unchanged runtime view** (source groups whose `rooms` are keyed by
   light-ref `"<room>.<id>"`, switches reattached by `governs`), so `coordinator.py`/`push.py` are
   structurally untouched and **addressing is byte-identical** (S1). On upgrade the Store **re-seeds from the
   new bundle** (overwrite is fine — no panel edits exist yet). `tests/test_seed_equivalence.py` pins the push
   plan (4 consolidated floods steady-state; a hold drops only its light group to per-light addressing). No
   runtime Store migrator yet — that lands in Phase 3 with editing. (`legacy_enable` is *not* on the stored
   source group — it was dropped as vestigial in v0.5.0.) 157 tests, 100% cov, mypy-strict + ruff clean.
2. **Panel + read-only (a usable read-only dashboard). ✅ DONE — `v0.7.0` (2026-06-11).** `panel.py` registers
   a `panel_custom` web component (require_admin) + serves the SPA from `panel/` via `async_register_static_paths`;
   `websocket.py` exposes two **read-only, admin-gated** commands — `light_man/config` (stored topology +
   switch map) and `light_man/subscribe` (live panel state pushed on every coordinator update). The SPA
   (`panel/index.js`) is **dependency-free vanilla JS** (no build step → HACS copy-paste stays clean; the Lit
   migration is deferred to Polish) with all 5 read-only tabs: Overview (live source-group tiles, push/sleep/
   alerts badges, holds), Rooms (lights+curve / switches→governs / sensors), Curves (hand-rolled SVG
   brightness-vs-elevation viz + CT/RGB color strip + "Used by"), Occupancy (zones, per-sensor sweeps),
   Activity (latest publish per topic). **The panel is optional** — guarded on `frontend`/`websocket_api`
   availability so a minimal install (or the test harness) loads the engine without it. Signatures were
   verified against current HA via the `ha-dev` agent. 163 tests, 100% cov, mypy-strict + ruff clean. **Zero
   write risk** (no write API). Confirmed live (clean boot, panel in sidebar).
3. **Editing + persistence.** Add the **write** API (config `POST` → Store) and the editors: the **curve
   editor** (all brightness/color fields + the per-curve **sunset wind-down / sunrise ramp rates**
   `edge_transition_s`/`wind_down_s` — see the Curves tab list above), **per-group curve assignment** (Rooms),
   the **sweep builder** (Occupancy), and the **Overview global settings** — `push_interval_s` and the **sleep
   ramp** (`ramp_in_s` ease-into-sleep / `ramp_out_s` ease-out-on-wake). Implement **Save & Export / Save As /
   Load config / Reset**; **flip the loader to migrate-the-Store-in-place** (M1) and add **`/XF
   light_man_config.json`** to the deploy mirror — both now load-bearing because the panel edits the Store;
   wire the **live linter**. Validation reuses the engine's own clamps (br 1–100, ct within bulb range, sat
   0–1, delays ≥0).
4. **Polish.** Curve/sweep visualization refinement, sweep **mirror/duplicate**, mobile layout,
   `require_admin` hardening.

## Architecture decisions (2026-06-11)

- **Delivery — `panel_custom` web component.** HA injects `hass` → free HA auth + the live websocket; mount a
  custom SPA inside. (Verify current panel / static-path / `websocket_api` signatures with the `ha-dev` agent
  before coding — these HA frontend APIs shift between releases.)
- **Frontend stack — Lit** (HA's own frontend is Lit; the panel entry *is* a web component, so HA's `ha-*`
  components + theming come for free) **+ hand-rolled SVG/canvas** for the curve + sweep visualizers (no
  heavy chart dependency). Keep the bundle tiny for HACS copy-paste. **v0.7.0 shipped a dependency-free
  *vanilla* web component instead** (a single `panel/index.js`, no build step) to land the read-only panel
  without wiring an npm/Vite pipeline; it reads HA CSS vars for theming and hand-rolls the SVG viz. The **Lit
  migration is deferred to Polish** (it needs the build pipeline below).
- **Apply semantics — stage + explicit Save, with live preview.** Edits stage locally; the curve redraws /
  sweep replays as preview only; nothing writes the Store or touches the house until Save.
- **Live transport — HA `websocket_api` custom commands** (subscribe to engine snapshot / diagnostics /
  occupancy / publish-log). The publish-log tail comes from the **coordinator** (it already sees all `/set`
  traffic), not a browser-side MQTT sub. No SSE.
- **Config read/write — `HomeAssistantView` HTTP GET/POST → the Store** (the live authority). Single-user
  house → **last-writer-wins**. **Save defaults to "Save & Export"** — each save writes the Store *and* a
  human-readable device config file, so config is durable + inspectable on the device with **no git commit
  required** (git is optional history only). **Save As** exports the current config to an external file
  (download); **Load config** imports + validates (+ migrates) an external file into the Store, to A/B-test
  and swap configs; **Reset** reverts to the immutable shipped factory default. Version bumps **migrate the
  Store in place** (M1), never overwrite. Once the panel owns device config, the deploy mirror must
  **exclude the live config file** (`robocopy … /XF light_man_config.json`) so code pushes never clobber it.
  Full detail + the four-artifact model in [`reference/data-model.md`](reference/data-model.md#config-persistence--files-dashboard).
- **Reset-to-seed — both** a global reset and per-curve / per-room "revert to seed."
- **Validation — derived from the engine's own clamps** (br 1–100, ct within bulb range, sat 0–1, delays ≥0);
  no bespoke rules.
- **Minimal HA entity surface kept** — `push_enable`, `sleep`, and a `force_push` button stay real HA
  entities for scripting/voice, in parallel with the panel.
- **Packaging** — `frontend/` source → `npm run build` → hashed bundle into
  `custom_components/light_man/panel/`, shipped in the integration (+ robocopy to live); CI lints/builds the
  panel without blocking the Python gate.
- **Auth — `require_admin`** on the panel registration + all write views.

## Current status (2026-06-11)

**Paused after Phase 2.** Phases 0 (design), 1 (schema, `v0.6.0`) and 2 (read-only panel, `v0.7.0`) are
shipped, deployed, and validated live on `dev`. The panel is a working **read-only** dashboard; it does not
yet write config. **Phase 3 (editing + persistence) is the next executable step** and has not started — see
the phase list above. Day-to-day tuning still goes through the git-bundled `light_man_config.json` until the
write API lands.

## Still open (deferred by design)

- **Read API endpoints** — *resolved in Phase 2*: `light_man/config` (WS request/response) + `light_man/subscribe`
  (WS subscription). The **write** endpoints are still deferred to Phase 3 (derived from the editor screens).
- **Per-tab wireframes/mockups** — *resolved*: the 5 read-only tabs are built (`panel/index.js`); the editor
  surfaces (curve editor, sweep builder, per-group curve assignment) are designed in the wireframes above and
  built in Phase 3.
- **Lit migration + npm/Vite build pipeline** — deferred to Polish (v0.7.0 shipped dependency-free vanilla JS).
