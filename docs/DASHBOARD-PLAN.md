# Light Man — Web Control Dashboard (plan)

**Status:** planning (added 2026-06-10). Tracked in [`POST-LAUNCH.md`](POST-LAUNCH.md).

## Goal

A nice, tabbed web dashboard that is the primary way to **operate and configure** Light Man — tune the
adaptive profiles, sleep, and occupancy live, with sensible controls (sliders, dropdowns, color pickers),
and **avoid a config/options flow** as much as possible. The static `light_man_config.json` seed becomes
the *initial* topology only; day-to-day tuning happens on the dashboard and persists.

## Approach: live config **entities**, not an options flow

The cleanest HA-native way to "configure from a dashboard" is for the integration to expose each tunable
as a first-class **entity** with `EntityCategory.CONFIG`, backed by the Store. The dashboard then just
binds cards to those entities — every change applies on the next push and survives restart. No YAML, no
options-flow modal.

New platforms to add to the integration:

| Platform | Used for |
|---|---|
| `number` | brightness min/max, color-temp min/max endpoints, sat, dusk-floor ct, night-floor br, sleep brightness/ct, ramp-in/out seconds, occupancy stage-delay / off-transition |
| `select` | per-source day/night color mode (`color_temp` / `rgb`), base color mode |
| `switch` | already have `push_enable` + `sleep`; add per-source **enable** and per-zone occupancy **enable** |
| `button` | `force_push`, `clear_holds` (wrap existing services as buttons) |
| `text` / `light`-style | base daytime color (RGB) — a color control; could be an `input_text` hex or a dedicated number triple |

Each writes through to the existing Store config (`SourceProfile` / `OccupancyZone`), so the engine reads
the same shapes it does today. The bundled JSON stays as the first-run seed + reset baseline.

### Why not the options flow
The options flow is a one-shot modal, can't show live engine state, and is clumsy for ~10 numbers ×
4 sources. Config entities are reactive, scriptable, and dashboard-friendly — and they let the dashboard
show the *current engine output* next to the controls that shape it.

## Dashboard layout (tabs)

A single dashboard, one view per tab:

1. **Overview** — `push_enable` + `sleep` toggles; live engine value per source (from the diagnostics
   sensor: brightness / color / mode); active-holds sensor + per-room expiry; `force_push` / `clear_holds`
   buttons; a "house at a glance" tile row.
2. **Profiles** — one section per source (overhead / accent / hallway up / hallway down): min/max
   brightness sliders, warm/cool color-temp endpoints, sat, dusk-floor ct, night-floor br, base daytime
   color (mode + RGB picker), and the `day_window` start/end + enable. Show the computed curve / current
   target alongside.
3. **Sleep** — global sleep toggle, ramp-in / ramp-out duration, and per-source sleep target (brightness,
   color mode, ct or rgb).
4. **Occupancy** — per-zone enable, stage-delay, off-transition; live occupancy state per sensor (from
   diagnostics); a note/link for the Z2M-side mmwave region tuning (out of scope for LM).
5. **Diagnostics** — render the coordinator diagnostics dict (issues, dedup-skips, addressing, last
   publish, engine, sleep, occupancy, inovelli) as entities/markdown; surface config `issues` prominently.

## Card strategy

- Core cards first (tile, entities, sections, conditional, markdown, history-graph) so it works with no
  custom-card dependency. Optionally upgrade to Mushroom / custom sliders if already installed.
- Group with the **sections** view type for responsive multi-column layout.
- Bind the diagnostics-driven readouts to a single diagnostics sensor whose attributes carry the engine
  snapshot (already computed in `_snapshot()` — may need a small read-only sensor that exposes it).

## Implementation phases

1. **Expose config entities (integration).** Add `number` / `select` / `button` platforms + per-source &
   per-zone enable switches, all `EntityCategory.CONFIG`, persisting to the Store. Tests + 100% cov.
2. **Diagnostics surface.** Add a read-only sensor (or attributes on an existing one) carrying the engine
   snapshot so the dashboard can display live targets and occupancy without the diagnostics download.
3. **Ship a dashboard.** A YAML dashboard (or a documented importable one) with the five tabs. Decide:
   bundle it with the integration (strategy dashboard) vs. ship as a copy-paste in docs.
4. **Reset/seed control.** A button to re-seed a source/zone from the bundled JSON (revert tuning).

## Open decisions (resolve at build time)

- **Color control** for the daytime base RGB: hex `text` entity vs. three `number`s vs. a helper `light`.
- **Granularity:** per-room overrides on the dashboard, or source-level only (today's model is
  source-level profiles + per-room holds).
- **Bundle vs. document** the dashboard — auto-provision a strategy dashboard, or keep it copy-paste so it
  doesn't fight the user's existing Lovelace setup.
- Whether config entities should be **enabled-by-default** (discoverable) or registry-disabled until the
  user opts in (keeps the entity list clean for non-dashboard users).
