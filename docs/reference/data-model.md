# Light Man — Reconciled Data Model (dashboard design)

**Status:** ✅ **implemented** (`v0.6.0`, 2026-06-11). This is the topology model the web control panel
(`docs/DASHBOARD-PLAN.md`) sits on; it supersedes the old source-centric shape in `light_man_config.json` /
`models.py`, which now hold this shape. The stored schema + the byte-identical runtime derivation shipped in
the Phase 1 schema migration (see "Staged work" below); the read-only panel that renders it shipped in Phase 2
(`v0.7.0`). This doc is now the **reference** for the stored shape, not a pending design.

Guiding principle: **complexity only where it buys functionality; preserve current behavior.** The review
that produced this revision removed the heaviest new complexity (per-light curve addressing) on exactly that
basis — see "Decisions" S1.

## Why this exists

The dashboard brief is **user-/room-centric and task-oriented**: present the **physical layout** (rooms hold
lights, switches, occupancy sensors) and reconcile it against the **control reality** — occupancy sensors
drive lights across rooms, switches bind to Zigbee groups or not, adaptive curves span many rooms. The user
must **define/edit the switch↔light↔curve↔occupancy relationships**, get **smart alerts on config
mismatches**, and **create/visualize multiple curves**.

The stored model doesn't match that. Today there is **no "room" object** (a "room" is a per-source addressing
slot), the four curves are **inline `profile` blocks glued one-per-source**, a physical room is **fragmented
across sources** (kitchen = an `overhead` room *and* an `accent` `kitchen_island` room), and occupancy
**sensors are redefined per zone** (`hall_west` duplicated in the hallway and stairwell zones).

## Terminology (two different "groups" — kept distinct on purpose)

| Term | What | Count today | Owns / is |
|---|---|---|---|
| **Source group** (`sources{}`) | the **curve-bearing consolidated unit** — formerly the inline-`profile` "source" | 4 | `consolidated_topic`, **`curve_ref`** (no `legacy_enable` — dropped v0.5.0) |
| **Light group** (`set_topic`) | a fixture's per-room `/set` topic — the **hold / SBM-bind / skip** unit | ~12 | the addressable bulbs a switch governs and a hold skips |

A light belongs to **one source group** (for its curve + the consolidated flood) and **is** a light group
(its `set_topic`, for holds). A switch **governs one light group**. Keeping these separate is what makes the
simplifications below behaviour-identical to today.

## The four overlay graphs

The same devices participate in four independent relationship graphs; the dashboard presents each and flags
inconsistencies *between* them.

1. **Physical containment (room-centric)** — Room ⊃ {lights, switches, sensors}. The human model; not stored today.
2. **Adaptive control (curve-centric)** — Curve → applies to → **source groups**. "What recipe does this fixture follow?"
3. **Occupancy control (zone-centric)** — Sensor(s) → staged sweep → lights; all-clear → off-targets. Cross-room; one sensor can feed several zones.
4. **Direct binding (Z2M / Inovelli SBM)** — Switch → bound light group → bulbs; owns on/off. **Not in Light Man config** — lives in Z2M. The dashboard *reads and validates* it, never edits it.

## Decisions (locked 2026-06-11)

### S1 — Curves → shared library, assigned **per source group** (not per light)
Curves become named, reusable objects; each **source group** carries a `curve_ref`. **No per-light/per-room
override in v1.** No current case needs one: even the hallway up/down split (`hallway_sky` vs
`hallway_ct_winddown`) is across **different source groups** (`zgb_hallway_up` vs `zgb_hallway_down`), not
within one. This keeps the **curve library + editor UX fully** while leaving runtime **addressing
byte-identical to today** (`push.plan_publishes` unchanged; held = skip, nothing else leaves the flood).
Per-light override + the addressing inversion it forces (consolidated flood valid only when a group is
curve-uniform → planning by `(topic, curve)`) are **deferred until a real need appears** — see "Addressing".

### S2 — Hold scope = the switch's **light group**, not the merged room
A hold keys on **the light group a tapped switch governs** (the group it's Inovelli-SBM-bound to), not on the
physical room. So rooms **merge for display** (kitchen shows both light groups, both switches, its sensor)
while a tap on the island switch holds **only** the island light group and the overhead switch **only** the
overhead light group — identical to today, with no per-light `arms` list. Each switch carries a single
`governs` reference to its light group.

### S3 / C1 — Sleep is split; no dedicated tab
Sleep is three separable things with natural homes: **global on/off** = runtime, exposed as
`switch.light_man_sleep` (Overview); **ramp-in/out timing** = one top-level `sleep` block (a global setting
on Overview); **per-curve sleep target** = on the curve, beside its day color (Curve editor). No dedicated
Sleep tab; nothing lost.

### Unchanged from the first cut
- **Curves are per-regime color-configurable.** Day color and Sleep color each toggle CT-ramp ↔ fixed-RGB —
  exactly what separates `hallway_up` (fixed sky-blue→orange) from `hallway_down` (CT day→fixed purple).
- **Room → first-class, one per physical space.** Owns `lights[]` (across source groups), `switches[]`, `sensors{}`.
- **Occupancy → two layers.** Room owns its sensors (defined once); a top-level **zone** *references* them
  (`"<room>.<sensor>"`), is cross-room, carries the all-clear off-targets.
- **Sweeps → inline on each (zone, sensor) binding.** Stages reference room fixtures by id (→ set_topic +
  curve), so they can't drift. (Editor **mirror/duplicate** sugar is deferred to the Polish phase.)

### Correction — held day/night looks are **derived from the curve**, not stored
`adaptive.day_look` = the curve at peak sun; `adaptive.night_look` = the curve at sun-down + sleep overlay
(`adaptive.py`); `_look_payloads` uses these for any profile source (`coordinator.py`). The per-source
`night_brightness_pct` / `night_color_temp_kelvin` / `night_rgb` fields are read **only for profile-less
sources** — none exist in the seed. They are **vestigial**; the migration drops them with **zero behavior
change**. No day/night regime needs adding to a curve.

## Target config shape

```jsonc
{
  "seed_version": 8,                            // bumped — re-seeds the Store from the new bundle (Phase 1)
  "push_interval_s": 30,

  "curves": {                                   // NEW top-level library (today's 4 profiles, named)
    "daylight_standard":   { "min_br":30,"max_br":90,"min_ct":2700,"max_ct":6500,"sat":0.5,
                             "base_color_mode":"color_temp","dusk_floor_ct":2200,"night_floor_br":30,
                             "sleep":{ "br":30,"color_mode":"color_temp","ct":2700 },
                             "day_window":{ "enabled":true,"start":"08:00","end":"17:00" } },
    "accent_dim":          { "min_br":10,"max_br":30,"min_ct":2700,"max_ct":6000,"sat":0.5,
                             "base_color_mode":"color_temp","dusk_floor_ct":2200,"night_floor_br":10,
                             "sleep":{ "br":10,"color_mode":"color_temp","ct":2500 }, "day_window":{ ... } },
    "hallway_sky":         { "min_br":30,"max_br":90,"sat":0.5,"base_color_mode":"rgb","base_rgb":[135,206,235],
                             "dusk_floor_ct":2200,"night_floor_br":30,
                             "sleep":{ "br":40,"color_mode":"rgb","rgb":[255,126,30] }, "day_window":{ ... } },
    "hallway_ct_winddown": { "min_br":30,"max_br":90,"min_ct":2700,"max_ct":6500,"sat":0.5,
                             "base_color_mode":"color_temp","dusk_floor_ct":2200,"night_floor_br":30,
                             "sleep":{ "br":5,"color_mode":"rgb","rgb":[155,123,232] }, "day_window":{ ... } }
  },

  "sources": {                                  // SOURCE GROUPS: the curve-bearing consolidated unit (S1)
    "overhead":     { "consolidated_topic":"zigbee2mqtt/zgb_overhead_all/set", "curve_ref":"daylight_standard" },
    "accent":       { "consolidated_topic":"zigbee2mqtt/zgb_accent_all/set",   "curve_ref":"accent_dim" },
    "hallway_up":   { "consolidated_topic":"zigbee2mqtt/zgb_hallway_up/set",   "curve_ref":"hallway_sky" },
    "hallway_down": { "consolidated_topic":"zigbee2mqtt/zgb_hallway_down/set", "curve_ref":"hallway_ct_winddown" }
  },

  "rooms": {                                    // NEW top-level physical spaces (merged across source groups)
    "kitchen": {                                // one physical room, two source groups, two switches
      "name": "Kitchen",
      "lights": [
        { "id":"overhead", "set_topic":"zigbee2mqtt/zgb_kitchen/set",        "source":"overhead" },
        { "id":"island",   "set_topic":"zigbee2mqtt/zgb_kitchen_island/set", "source":"accent" }
      ],
      "switches": [
        { "topic":"zigbee2mqtt/Kitchen Overhead Light Switch", "governs":"kitchen.overhead" },  // holds zgb_kitchen only (S2)
        { "topic":"zigbee2mqtt/Kitchen Island Light Switch",   "governs":"kitchen.island"   }   // holds zgb_kitchen_island only
      ],
      "sensors": {}
    },
    "hallway": {
      "name": "Hallway",
      "lights": [
        { "id":"east_up",   "set_topic":"zigbee2mqtt/Hallway Light East Uplight/set",   "source":"hallway_up" },
        { "id":"east_down", "set_topic":"zigbee2mqtt/Hallway Light East Downlight/set", "source":"hallway_down" }
        /* center_up/down, west_up/down — 6 fixtures across the two hallway source groups */
      ],
      "switches": [],                           // the hall mmwave devices are sensors here, not holdable switches
      "sensors": {
        "hall_east": { "topic":"zigbee2mqtt/Hallway Light Switch East New New mmwave","occupancy_key":"mmwave_area1_occupancy" },
        "hall_west": { "topic":"zigbee2mqtt/Hall Smart Switch West New mmwave","occupancy_key":"mmwave_area1_occupancy" }
      }
    }
    /* living_room, primary_bedroom, office, stairwell, front_door (1 light), michael_closet (1 light), ... */
  },

  "occupancy_zones": {                          // logical, cross-room; reference room-owned sensors
    "hallway": {
      "sensors": {
        "hallway.hall_east": { "sweep":[ { "lights":["hallway.east_up","hallway.east_down"] },
                                         { "delay_s":1.0, "lights":["hallway.center_up","hallway.center_down"] },
                                         { "delay_s":1.0, "lights":["hallway.west_up","hallway.west_down"] } ] },
        "hallway.hall_west": { "sweep":[ /* mirror: west → center → east */ ] }
      },
      "off_lights":["zigbee2mqtt/zgb_hallwayf/set"], "off_transition_s":1.5   // zgb_hallwayf is a real group (kept as-is)
    },
    "stairwell": {
      "sensors": {
        "stairwell.stairwell_top": { "sweep":[ { "lights":["stairwell.overhead"] } ] },
        "hallway.hall_west":       { "sweep":[ { "lights":["stairwell.overhead"] } ] }  // SAME sensor, other zone
      },
      "off_lights":["zigbee2mqtt/zgb_stairwell/set"]
    }
  },

  "sleep": { "ramp_in_s": 5400, "ramp_out_s": 1800 }   // global ramp timing; on/off is runtime (switch.light_man_sleep)
}
```

## Element detail

### Curves (library)
The elevation→target recipe: brightness endpoints (`min_br`/`max_br`/`sat`/`night_floor_br`) + `dusk_floor_ct`
+ the two color regimes + the sleep **target** + optional `day_window`. Math unchanged — see
`adaptive-algorithm.md` / `custom_components/light_man/adaptive.py`. The schema change is **lifting the four
inline `profile`s into a named top-level `curves{}` map** that source groups reference.
- **Day color** = `base_color_mode` (`color_temp` ⇒ `min_ct`/`max_ct` ramp | `rgb` ⇒ fixed `base_rgb`).
- **Sleep color** = `sleep.color_mode` (`color_temp` ⇒ `sleep.ct` | `rgb` ⇒ fixed `sleep.rgb`).
- **Held looks are derived** (peak-sun day, sun-down+sleep night) — not stored on the curve.
- **Editor:** brightness line across the solar arc; color strip = CT gradient or flat RGB swatch per regime; a
  **summer/equinox/winter** selector (seasonal swing is real — `REF=71.5°` fixed); sleep-ramp preview; a
  **"Used by"** backref (which source groups → edit blast radius).

### Source groups (`sources{}`)
The curve-bearing consolidated unit: `consolidated_topic`, `curve_ref` (optional `transition_s`). Its **members are
derived** — every room-light whose `source` names it. This is the unit the consolidated flood targets and the
unit a curve is assigned to (S1). The four recognizable groups remain visible in the UI.

### Rooms (first-class)
`rooms{}` keyed by id, each with a display `name`. Owns:
- `lights[]` — each `{ id, set_topic, source }`; its curve = `sources[source].curve_ref`. A room may hold
  lights from several source groups (kitchen overhead + island).
- `switches[]` — each `{ topic, governs }` where `governs` is `"<room>.<light_id>"` — the **light group it is
  SBM-bound to and holds** (S2). Switch writes are brightness-only (`defaultLevel` + LED bar), the brightness
  coming from the governed light's source-group curve.
- `sensors{}` — the mmwave devices **mounted in this room** (`{topic, occupancy_key}`), defined once.

### Holds
Key on the **governed light group** (`"<room>.<light_id>"` → its `set_topic`) — the same skip granularity as
today's per-room hold; merging rooms for display does not widen it. Arm/release/TTL semantics unchanged
(ARCHITECTURE §5.3).

### Occupancy (two layers)
- **Sensor (physical):** owned by the room it's mounted in; defined once; identified house-wide as `"<room>.<sensor>"`.
- **Zone (logical):** top-level, cross-room; references room sensors and attaches a **per-sensor sweep**;
  carries `off_lights` + `off_transition_s`; clears when **all** its sensors report empty. A sensor may be
  referenced by several zones with a different sweep each (`hall_west` → hallway sweep + stairwell-overhead sweep).

### Sweeps (inline)
Ordered stages `{ delay_s, lights[] }`; each light is a **room-fixture reference** → its `set_topic` + curve. A
stage target may be a switch. Lives on the `(zone, sensor)` binding (where `coordinator.py` `_Binding` already
flattens it). Builder = a **timeline** (stages, per-stage delay, drag room fixtures/switches in) with live
replay; **mirror/duplicate** are Polish-phase sugar.

### Sleep (split — S3/C1)
- Per-curve **target** (`sleep.br` + color) lives on the curve.
- Global **ramp** timing (`ramp_in_s`/`ramp_out_s`) → top-level `sleep` block.
- **On/off** is runtime, exposed as `switch.light_man_sleep` (the minimal-entity-surface decision).

## Addressing — unchanged from today (S1)

With curves at the **source-group** level, the consolidated flood is always valid: every member of a group
resolves to the **same** curve. So `coordinator.py` / `push.plan_publishes` stays exactly as-is — **no held
room** → one consolidated flood (4 floods steady state); **a switch's light group held** → publish per-unheld
light group, skipping the held one (the only reason a member leaves the flood). No per-curve planning, no
`(topic, curve)` membership.

> **Deferred (forward note):** if per-light/per-room curve override is ever wanted, it reintroduces the
> **addressing inversion** — a curve-divergent member must be published individually at its own curve (the
> hold machinery generalized: a hold *skips*, an override *publishes with a different curve*), and the
> consolidated flood becomes the fast path only for curve-uniform groups. Out of scope for v1.

## Config linter (the "smart alerts")

Cross-checks the reconciled model against live Z2M; surfaceable as HA repair issues later.
- Holdable light group (a switch governs it) with **no valid `set_topic`** (already enforced in `config_loader`).
- `curve_ref` naming **no library curve**.
- Sweep stage referencing a **light/switch owned by no room**; zone referencing a sensor **owned by no room**;
  a room sensor used by **no zone** (orphan).
- Sensor `topic` / `occupancy_key` **not exposed** by that Z2M device.
- **`off_lights` topic resolving to no known Z2M group** (catches an off-target that names no real group).
- Source-group member **missing `hue_native_control`** (ARCHITECTURE §5.5 orphan coverage).
- Switch **bound (Z2M) to a different light group** than the one it `governs` (paddle drives other bulbs).
- Bulb in a per-room light group but **not** in its source group's consolidated group (stale-NVRAM bulb-split
  class — `bulb-split-investigation.md`).

## Old → new migration mapping (behaviour-preserving)

| Today (`light_man_config.json`) | New |
|---|---|
| `sources.<s>.profile` (×4) | `curves.<name>` (×4), referenced by `sources.<s>.curve_ref` (S1) |
| `sources.<s>` (consolidated_topic) | kept on the source group as `consolidated_topic`+`curve_ref`; loses `profile`/`rooms` |
| `sources.<s>.rooms.<r>` (set_topic, switches) | `rooms.<r>` with a light `{set_topic, source:<s>}` + switches `{topic, governs}` |
| same physical room under 2 sources (kitchen / kitchen_island) | **one** `rooms.kitchen` with two lights + two switches, **holds preserved per light group** (S2) |
| `sources.<s>.night_brightness_pct/_color_temp_kelvin/_rgb` | **dropped** — vestigial (held looks derived; profile-less only) |
| `occupancy.<z>.sensors.<n>` (inline topic+key+sweep) | sensor → `rooms.<room>.sensors.<n>`; zone keeps a **reference** + the sweep |
| `hall_west` defined in 2 zones | **one** room-owned sensor, referenced by both zones |
| sweep light `{set_topic, source}` | room-fixture id (`"<room>.<id>"`); curve derived from the fixture's source group |
| (sleep ramp constants, per-source `sleep`) | top-level `sleep` block (ramp) + per-curve `sleep` target |

### Migration mechanics (M1 — Store-seeded, not bundle-read at runtime)
Config loads from a `Store` **seeded** from the bundled JSON, so existing installs hold the **old shape
persisted**. Three obligations:
1. Rewrite the bundled `light_man_config.json` to the new shape with a **bumped `seed_version`** (→ 7).
2. The loader detects an **old-shape Store** (by `seed_version`) and migrates it **in place** to the new shape.
3. The behaviour-preserving test runs the migration on the **old** seed and asserts the resulting push plan —
   **including hold addressing** — equals the new seed's ("4 floods, byte-identical").

### `switch_map` shape (M2 — doc note; code is Phase 1b)
`switch_map` becomes `dict[base, (room, light_group)]` (was `(source, room)`); consumers at
`coordinator.py:202,756,770` resolve a tapped switch to its physical room + the governed light group it holds.

### Config persistence & files (dashboard)

Four artifacts, distinct roles. The panel makes the **Store the live authority** — config is durable on the
device and needs **no git commit** to work:

| Artifact | Role | Written by |
|---|---|---|
| **Store** (`.storage/light_man_config`) | the **live authority**; survives restarts | every Save |
| **Device config file** (`light_man_config.json`) | human-readable mirror of the live config | **Save & Export** (the default Save) |
| **Shipped factory default** (git-bundled seed) | first-run bootstrap + **Reset** baseline; immutable on the device | shipped with the integration |
| **External snapshot** (download/upload) | a portable config to test/swap | **Save As** (export) / **Load config** (import) |

- **Save = "Save & Export"** (the default action): writes the Store **and** the device config file in one go,
  so the config is durable + inspectable **without any git commit** (git is optional history/sharing only).
- **Save As**: export the current config to an external file (browser download) — keep multiple named snapshots.
- **Load config**: import an external file → **validate (+ migrate if older)** → replace the Store. Lets you
  **A/B-test and swap** whole configs. Last-writer-wins.
- **Reset**: revert to the immutable shipped factory default (global, or per-curve/per-room).
- **Migrate, never overwrite (M1):** on a `seed_version` bump the loader **migrates the Store in place**; it
  must NOT overwrite a panel-edited Store from the bundle (today's `__init__.py` overwrite-on-bump becomes a
  migration once the panel can write the Store).
- **Deploy must not clobber device config:** once the panel owns the device config file, the code mirror
  **excludes it** (`robocopy … /XF light_man_config.json`); until then the git seed is authoritative and
  deploys normally.

## Staged work (after sign-off)

Phasing is **canonical in [`DASHBOARD-PLAN.md`](../DASHBOARD-PLAN.md#implementation-phases)** (Phases 0–4).
Where this doc's pieces land:

- **Phase 1 (schema migration): ✅ shipped in `v0.6.0` (2026-06-11).** The new schema landed in `models.py`
  (stored types `Curve`/`SourceGroup`/`Room`/`RoomLight`/`RoomSwitch`/`RoomSensor`/`ZoneConfig`/`SleepConfig`
  +`StoredConfig`) and `config_loader.py` **derives the unchanged runtime view** (source groups whose `rooms`
  are keyed by light-ref `"<room>.<id>"`, switches reattached by `governs`), so `coordinator.py`/`push.py` are
  structurally untouched and addressing is byte-identical (S1). `light_man_config.json` was rewritten to the
  new shape; on upgrade the Store **re-seeds from the new bundle** (overwrite is fine — no panel edits exist
  yet). `tests/test_seed_equivalence.py` pins the push plan (4 consolidated floods steady-state; a hold drops
  only its light group to per-light addressing). **No runtime Store migrator yet.** Two deltas from the shape
  sketched below: the shipped **`seed_version` is `8`** (not 7 — v7 was the prior AL-fallback cleanup), and
  **`legacy_enable` is omitted** from source groups (it was dropped as vestigial in v0.5.0; the engine never
  reads it). The **M2** `switch_map` shape (`base → (group, light_ref)`) shipped as designed.
- **Phase 3 (editing + persistence):** the **in-place migration (M1)** flip and the `/XF` deploy exclusion —
  both only load-bearing once the panel writes the Store. See "Config persistence & files" above.

## See also
- `docs/DASHBOARD-PLAN.md` — the panel (delivery, IA, build pipeline, phases).
- `docs/reference/adaptive-algorithm.md` — the curve math the editor visualizes.
- `docs/reference/ARCHITECTURE.md` §5 — addressing / hold semantics (per-group curves keep §5.1 as-is; §5.3 hold scope = light group).
- `docs/reference/bulb-split-investigation.md` — the stale-NVRAM class the linter surfaces.
