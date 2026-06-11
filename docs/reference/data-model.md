# Light Man — Reconciled Data Model (dashboard design)

**Status:** design, sign-off pending (added 2026-06-11). This is the target topology model the web control
panel (`docs/DASHBOARD-PLAN.md`) is built on. It supersedes the source-centric shape currently in
`custom_components/light_man/light_man_config.json` / `models.py`. **No code/schema change lands until this
design is signed off** (per the design-first rule in `DASHBOARD-PLAN.md`); this doc is the artifact being
reviewed.

## Why this exists

The dashboard brief is **user-/room-centric and task-oriented**: it must present the **physical layout**
(rooms hold lights, switches, occupancy sensors) and reconcile it against the **control reality** —
occupancy sensors drive lights across rooms, switches bind to Zigbee groups or not, and adaptive "curves"
span many rooms. The user must be able to **define/edit the switch↔light↔curve↔occupancy relationships**,
get **smart alerts on config mismatches**, and **create/visualize multiple adaptive curves**.

The stored model doesn't match that mental model. Today:

- there is **no "room" object** — a "room" is just a per-source addressing slot (`SourceConfig.rooms`);
- the four adaptive **curves are inline `profile` blocks, one glued per source** — not nameable or reusable;
- a physical room is **fragmented across sources** (the kitchen is an `overhead` room *and* an `accent`
  `kitchen_island` room);
- occupancy **sensors are redefined per zone** — `hall_west` is duplicated in the `hallway` and `stairwell`
  zones.

So the panel needs a reconciled model underneath it. This doc defines it.

## The four overlay graphs

The same physical devices participate in four independent relationship graphs. The dashboard's job is to
present each and flag inconsistencies *between* them.

1. **Physical containment (room-centric)** — Room ⊃ {lights, switches, occupancy sensors}. The human mental
   model. *Not* stored as such today.
2. **Adaptive control (curve-centric)** — Curve → applies to → {room-lights, switches}. "What recipe does
   this fixture follow?"
3. **Occupancy control (zone-centric)** — Sensor(s) → staged sweep → lights; all-clear → off-targets.
   Inherently cross-room; one sensor can feed several zones.
4. **Direct binding (Z2M / Inovelli SBM)** — Switch → bound Zigbee group → bulbs; owns on/off. **Not in
   Light Man config at all** — lives in Z2M. The dashboard *reads and validates* it, never edits it.

## Decisions (locked 2026-06-11)

1. **Curves → shared library, assigned per target.** Curves become named, reusable objects. Sources and
   **rooms** reference one (`curve_ref`); a room override beats the source default. **No per-switch
   override** — switches stay `list[str]`, inherit their room's curve, and consume only the curve's
   **brightness** channel (`defaultLevel` + LED bar), never color.
2. **Curves are per-regime color-configurable.** Each curve sets **Day color** and **Sleep color**
   independently, each either an elevation **CT ramp** (`min_ct`→`max_ct`) or a **fixed RGB** swatch
   (`base_rgb` / `sleep.rgb`). Brightness endpoints always apply; the CT-only knobs are inert (grey out) when
   a regime is fixed-RGB. This is exactly what separates the two hallway curves (below).
3. **Room → first-class top-level object, one per physical space.** Each room owns `lights[]` (many — incl.
   from multiple sources), `switches[]`, and `sensors{}`. Single-bulb "rooms" become rooms with one light.
4. **Source → dissolved.** Each room-light carries its own `curve_ref` + an RF/addressing **group tag**
   (`consolidated_topic`, `legacy_enable`). "Source" survives only as a **derived grouping** — the set of
   lights sharing a consolidated topic — used purely for flood addressing.
5. **Occupancy → two layers.** **Physical:** a room owns its mmwave **sensors** (defined once). **Logical:**
   a top-level **zone** *references* room-owned sensors (`"<room>.<sensor>"`), is cross-room, and carries the
   all-clear off-targets. De-duplicates shared sensors.
6. **Sweeps → inline on each (zone, sensor) binding** (not a named library). The sweep is what *this sensor,
   on behalf of this zone* runs. Editor offers **duplicate** + **mirror/reverse** (the two hallway sensors
   are mirror images). Stages reference **room fixtures** by id (→ set_topic + curve); a stage target may be
   a **switch** as well as a light.

## Target config shape

```jsonc
{
  "curves": {                                   // NEW top-level library (today's 4 profiles, named)
    "daylight_standard":   { "min_br":30,"max_br":90,"min_ct":2700,"max_ct":6500,"sat":0.5,
                             "base_color_mode":"color_temp", "sleep":{...}, "day_window":{...} },
    "accent_dim":          { "min_br":10,"max_br":30,"min_ct":2700,"max_ct":6000, ... },
    "hallway_sky":         { "base_color_mode":"rgb","base_rgb":[135,206,235],
                             "sleep":{"color_mode":"rgb","rgb":[255,126,30]} },     // fixed RGB both regimes
    "hallway_ct_winddown": { "base_color_mode":"color_temp",
                             "sleep":{"color_mode":"rgb","rgb":[155,123,232]} }     // CT day → fixed RGB sleep
  },

  "rooms": {                                    // NEW top-level physical spaces
    "hallway": {
      "name": "Hallway",
      "lights": [
        { "id":"east_up",   "set_topic":"zigbee2mqtt/Hallway Light East Uplight/set",
          "curve_ref":"hallway_sky",
          "group":{ "consolidated_topic":"zigbee2mqtt/zgb_hallway_up/set",
                    "legacy_enable":"input_boolean.adaptive_lighting_switch_hallway" } },
        { "id":"east_down", "set_topic":"zigbee2mqtt/Hallway Light East Downlight/set",
          "curve_ref":"hallway_ct_winddown",
          "group":{ "consolidated_topic":"zigbee2mqtt/zgb_hallway_down/set",
                    "legacy_enable":"input_boolean.adaptive_lighting_switch_hallway" } }
        /* center_up/down, west_up/down — 6 fixtures total */
      ],
      "switches": ["zigbee2mqtt/Hall Smart Switch West New mmwave"],
      "sensors": {
        "hall_east": { "topic":"zigbee2mqtt/Hallway Light Switch East New New mmwave",
                       "occupancy_key":"mmwave_area1_occupancy" },
        "hall_west": { "topic":"zigbee2mqtt/Hall Smart Switch West New mmwave",
                       "occupancy_key":"mmwave_area1_occupancy" }
      }
    }
    /* living_room, kitchen (overhead + island lights), primary_bedroom, office, stairwell, front_door, ... */
  },

  "occupancy_zones": {                          // logical, cross-room; reference room-owned sensors
    "hallway": {
      "sensors": {
        "hallway.hall_east": { "sweep":[ { "lights":["hallway.east_up","hallway.east_down"] },
                                         { "delay_s":1.0, "lights":["hallway.center_up","hallway.center_down"] },
                                         { "delay_s":1.0, "lights":["hallway.west_up","hallway.west_down"] } ] },
        "hallway.hall_west": { "sweep":[ /* mirror: west → center → east */ ] }
      },
      "off_lights":["zigbee2mqtt/zgb_hallwayf/set"], "off_transition_s":1.5
    },
    "stairwell": {
      "sensors": {
        "stairwell.stairwell_top": { "sweep":[ { "lights":["stairwell.overhead"] } ] },
        "hallway.hall_west":       { "sweep":[ { "lights":["stairwell.overhead"] } ] }  // SAME sensor, other zone
      },
      "off_lights":["zigbee2mqtt/zgb_stairwell/set"]
    }
  }
}
```

## Element detail

### Curves (library)

A curve is the elevation→target recipe: brightness endpoints (`min_br`/`max_br`/`sat`/`night_floor_br`) +
the two color regimes + the sleep overlay + optional `day_window`. The math is unchanged — see
`docs/reference/adaptive-algorithm.md` and `custom_components/light_man/adaptive.py`. The only schema change
is **lifting the four inline `profile`s into a named top-level `curves{}` map** that targets reference.

- **Day color** = `base_color_mode` (`color_temp` ⇒ `min_ct`/`max_ct` ramp | `rgb` ⇒ fixed `base_rgb`).
- **Sleep color** = `sleep.color_mode` (`color_temp` ⇒ `sleep.ct` | `rgb` ⇒ fixed `sleep.rgb`).
- **Editor visualization:** brightness line across the solar arc (always), plus a color strip rendered as a
  CT gradient or a flat RGB swatch per regime; a **summer / equinox / winter** selector (the seasonal swing
  is real — `REF=71.5°` is fixed); a sleep-ramp preview. A **"Used by"** backref lists every source/room/
  switch on the curve so edit blast-radius is visible.

### Rooms (first-class)

`rooms{}` keyed by an id, each with a display `name`. Owns:
- `lights[]` — each a fixture/group with its `id`, `set_topic`, `curve_ref` (override; else inherits its
  group default), and a `group` RF tag (`consolidated_topic` + `legacy_enable`). A room may hold lights from
  several groups (kitchen overhead + island accent).
- `switches[]` — Inovelli base topics whose `action`/`state` arm+release holds and receive `defaultLevel`/
  LED-bar writes (brightness-only).
- `sensors{}` — the mmwave devices **mounted in this room** (`{topic, occupancy_key}`), defined once.

### Source (dissolved → derived grouping)

There is no `source` object. A "source" is the **set of room-lights sharing a `group.consolidated_topic`**,
materialized at load for addressing + the legacy-enable reconcile. Each light's `curve_ref` (its own, or its
group default) gives its recipe. The four recognizable sources remain visible in the UI as derived groups.

### Occupancy (two layers)

- **Sensor (physical):** owned by the room it's mounted in. Defined once; identified house-wide as
  `"<room>.<sensor>"`.
- **Zone (logical):** top-level, cross-room. References room-owned sensors and attaches a **per-sensor
  sweep**; carries `off_lights` + `off_transition_s`; clears (off-targets fire) when **all** its sensors
  report empty. A sensor may be referenced by several zones with a different sweep each (`hall_west` → a
  hallway sweep in `hallway`, a stairwell-overhead sweep in `stairwell`).

### Sweeps (inline)

A sweep is an ordered list of stages `{ delay_s, lights[] }`; each light is a **room-fixture reference**
resolving to its `set_topic` + curve (so a sweep can't drift from the room's fixtures). A stage target may
be a switch. It lives on the `(zone, sensor)` binding — exactly where the coordinator already flattens it
(`coordinator.py` `_Binding`). The builder is a **timeline** (stages left→right, per-stage delay, drag room
fixtures/switches in) with a live replay and **duplicate / mirror** actions.

## Addressing implication (caught in the push code)

`coordinator.py` / `push.plan_publishes` floods one value to the consolidated group when no room is held,
and only addresses per-room to **skip held rooms**. With dissolved sources + **per-room curve overrides**,
the consolidated flood is valid **only when every member of a group resolves to the same curve**. A
curve-overridden room must be published individually at its own value — the **same machinery as a hold,
generalized**:

| reason a member leaves the consolidated flood | what happens to it |
|---|---|
| **held** (scene owns it) | **skipped** — not published |
| **curve-divergent** (room override ≠ group default) | **published per-room** at its own curve |

Net rule for the build: consolidated flood stays the fast path for a **curve-uniform** group; any held or
curve-divergent member drops (that member, or the group) to per-room addressing. With the behaviour-
preserving migration (every room on its group default) this is **identical to today** — overrides are opt-in
complexity.

## Config linter (the "smart alerts")

Cross-checks the reconciled model against live Z2M; surfaceable as HA repair issues later.

- Holdable room (has switches) with **no per-room `set_topic`** (already enforced in `config_loader`).
- `curve_ref` naming **no library curve**; a room override **identical** to its group default (info only).
- Sweep stage referencing a **light/switch owned by no room**; zone referencing a sensor **owned by no
  room**; a room sensor used by **no zone** (orphan).
- Sensor `topic` / `occupancy_key` **not exposed** by that Z2M device.
- Consolidated-group member **missing `hue_native_control`** (ARCHITECTURE §5.5 orphan coverage).
- Switch in a room **bound (Z2M) to a different group** than its room's light group (paddle drives other
  bulbs than the curve).
- Bulb in a per-room group but **not** in the consolidated group (stale-NVRAM bulb-split class —
  `docs/reference/bulb-split-investigation.md`).

## Old → new migration mapping (behaviour-preserving)

| Today (`light_man_config.json`) | New |
|---|---|
| `sources.<s>.profile` (×4) | `curves.<name>` (×4), referenced by `curve_ref` |
| `sources.<s>.consolidated_topic` / `legacy_enable` | each member light's `group` tag |
| `sources.<s>.rooms.<r>` (set_topic, switches) | `rooms.<r>` light(s) + `switches[]`, lights tagged with the source's group + curve |
| same physical room under 2 sources (kitchen / kitchen_island) | one `rooms.kitchen` owning both groups' lights |
| `occupancy.<z>.sensors.<n>` (inline topic+key+sweep) | sensor → `rooms.<room>.sensors.<n>`; zone keeps a **reference** + the sweep |
| `hall_west` defined in 2 zones | one room-owned sensor, referenced by both zones |
| sweep light `{set_topic, source}` | room-fixture id (`"<room>.<id>"`), curve derived |

A loader migration performs this pivot from the current seed; a test asserts the resulting push plan for the
no-hold / no-override steady state is byte-identical (4 consolidated floods).

## Staged work (after sign-off)

1. **Schema + migration** — `models.py` (`Curve`/`curves`, first-class `Room`, light `curve_ref`+group tag,
   `OccupancyZone.sensors` → references, sweep `lights` → fixture ids); `config_loader.py` old→new migration;
   `light_man_config.json`. Tests vs. the existing seed.
2. **Coordinator addressing** — derive group members from room-lights; generalize `plan_publishes` for
   curve-divergent members; resolve sweep fixture refs. Push payloads/MQTT behaviour unchanged.
3. **Backend API → panel registration/static serving → frontend SPA → polish** — per `DASHBOARD-PLAN.md`
   phases 2–6 (read-only surfaces first, then editors + linter).

## See also

- `docs/DASHBOARD-PLAN.md` — the panel (delivery mechanism, IA, build pipeline, phases).
- `docs/reference/adaptive-algorithm.md` — the curve math the editor visualizes.
- `docs/reference/ARCHITECTURE.md` §5 — addressing / hold semantics the override rule generalizes.
- `docs/reference/bulb-split-investigation.md` — the stale-NVRAM class the linter surfaces.
