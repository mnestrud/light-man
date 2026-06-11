# Light Man

Home Assistant custom integration — a house-wide **adaptive-lighting orchestrator** for a
Zigbee2MQTT + Philips Hue + Inovelli Blue (VZM31-SN / VZM32-SN) system.

Light Man is the single adaptive brain for the house's bulbs: it computes each source's target from the
**real solar elevation**, owns the per-source push, a per-room **hold** model, a global **sleep** overlay,
and **mmwave occupancy** lighting. It talks to **Zigbee2MQTT over MQTT only** — Z2M keeps owning groups,
bindings, `hue_native_control`, and the Inovelli Smart Bulb Mode bindings.

## What it does

- **Real-elevation adaptive engine.** Every ~30s it reads the sun's current elevation (and today's noon
  elevation, for seasonal honesty) and computes each source's brightness + color from its **profile**
  (min/max brightness, warm/cool color-temp endpoints, an optional fixed daytime color such as the
  hallway sky-blue, dusk/night floors). Brightness is interpolated perceptually; color in mireds. It then
  floods the consolidated Zigbee group, addressing **per-room** only when a room diverges (held/off).
  A source with no profile falls back to an Adaptive Lighting "dummy" switch (optional, legacy).
- **Per-room holds** driven by explicit Inovelli paddle actions: a Config tap holds the room at the
  engine's **night** (single) or a distinct forced **day** look (double); a single up/down tap releases
  it back to adaptive; a held-dim freezes it (the hardware binding owns the ramp). Holds auto-expire at
  the next solar midnight and survive restarts.
- **Global sleep overlay.** A sleep toggle ramps the whole house into each source's per-source sleep
  target (warm/dim) over a configurable ramp (default 90 min in / 30 min out), and back out on wake.
- **mmwave occupancy.** Subscribes to each Inovelli Blue mmwave sensor's `occupancy` over MQTT and runs a
  per-sensor **directional sweep** (staggered stages, e.g. hallway east→center→west) at the live engine
  value; turns the zone off when all its sensors clear.
- **Inovelli prestage.** Pushes each switch's `defaultLevelLocal/Remote` to the live adaptive brightness
  (so a tap-on comes up at the right level) plus the LED-bar brightness while the paddle is on.
- **One master toggle** turns Light Man on or off — ON runs the adaptive push, OFF makes it inert. It
  defaults ON and restores across restarts, so Light Man is the house default.

Full design: [`docs/reference/adaptive-algorithm.md`](docs/reference/adaptive-algorithm.md).

## Requirements

- Home Assistant **2026.1+**.
- The **MQTT** integration configured (Zigbee2MQTT).
- Adaptive Lighting (HACS) is **not required** — Light Man computes every value from real solar elevation
  and is fully self-contained. Any legacy AL / master-tick automations should be disabled; Light Man no
  longer manages them.

## Installation

- **HACS (custom repository):** add `https://github.com/mnestrud/light-man` as an *Integration*
  repository, install **Light Man**, restart HA.
- **Manual:** copy `custom_components/light_man/` into your HA `config/custom_components/`, restart HA.

Then **Settings → Devices & Services → Add Integration → Light Man**. It is a single-instance
integration and the setup step is a **confirm only** — there are no parameters to enter. On first run
it seeds its topology from the bundled `light_man_config.json`.

## Configuration

There is no UI options flow yet (Phase 2). The topology + per-source profiles load from a Store-seeded
JSON (`custom_components/light_man/light_man_config.json`), keyed by **source**:

| Field | Meaning |
|---|---|
| `consolidated_topic` | the source's group `/set` topic (steady-state flood) |
| `profile.min_br` / `max_br` | brightness endpoints (%) across the elevation range |
| `profile.min_ct` / `max_ct` | warm/cool color-temp endpoints (Kelvin) |
| `profile.base_color_mode` / `base_rgb` | optional fixed daytime color (e.g. hallway sky-blue) |
| `profile.dusk_floor_ct` / `night_floor_br` | warm/dim floors at low elevation |
| `profile.sleep` | per-source sleep target (`br`, `color_mode`, `ct`/`rgb`, ramp overrides) |
| `profile.day_window` | optional forced sunrise/sunset clamp (`start`/`end` "HH:MM") |
| `transition_s` | optional per-source transition time (seconds; default 1.0) |
| `rooms.<room>.set_topic` | per-room group `/set` (addressed when the room is held/off) |
| `rooms.<room>.switches` | Inovelli switch base topic(s) whose actions arm/release the hold |
| `occupancy.<zone>` | mmwave sensor topics, per-sensor directional sweep, off-lights |

## Entities

- **`switch.light_man_adaptive_push`** — the master on/off toggle (ON = Light Man runs the adaptive push;
  OFF = inert). Defaults ON; restored across restarts.
- **Sleep switch** (`*_sleep_enable`) — global sleep overlay; ramps the house into its sleep target.
- **Active-holds sensor** — count of currently-held rooms; attributes list each room and its expiry.

## Actions (services)

| Service | What it does |
|---|---|
| `light_man.release_hold` | release one room's hold (`room:` field) back to adaptive |
| `light_man.clear_holds` | release every held room |
| `light_man.force_push` | re-publish every source now, bypassing write-on-change dedup |

## Removal

**Settings → Devices & Services → Light Man → ⋮ → Delete.** Light Man goes inert on unload (it does not
manage any legacy stack). To fully remove, also delete `custom_components/light_man/` from
`config/custom_components/` and restart.

## Status & quality

Phase 2 delivered and live: the real-elevation engine drives the house as the default, with sleep and
mmwave occupancy absorbed. Quality scale: **Silver** — see
[`memory/light_man_audit.md`](memory/light_man_audit.md).

## Branch / release workflow

- `dev` — default working branch; pushed before each deploy to live HA.
- `main` — stable releases only; merged from `dev` via PR and tagged per numbered release.

## License

MIT — see [LICENSE](LICENSE).
