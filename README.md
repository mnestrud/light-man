# Light Man

Home Assistant custom integration — a house-wide **adaptive-lighting orchestrator** for a
Zigbee2MQTT + Philips Hue + Inovelli Blue (VZM31-SN / VZM32-SN) system.

Light Man is the single adaptive brain for the house's bulbs: it owns the per-source adaptive push
and a per-room **hold** model so a manually-set room scene survives the next adaptive tick. It talks
to **Zigbee2MQTT over MQTT only** — Z2M keeps owning groups, bindings, `hue_native_control`, and the
Inovelli Smart Bulb Mode bindings.

## What it does

- **Owns the adaptive push** for each source (overhead / accent / hallway up & down): every ~30s it
  reads the live adaptive values (from Adaptive Lighting "dummy" switches) and floods the consolidated
  Zigbee groups, addressing per-room only when a room is held.
- **Per-room holds** driven by explicit Inovelli paddle actions: a Config tap holds the room at Light
  Man's night/day target; a single tap (up or down) releases it back to adaptive. Holds auto-expire at
  the next solar midnight and survive restarts.
- **One toggle** swaps the whole stack between Light Man and the legacy tick automation.

Full design: [`docs/reference/ARCHITECTURE.md`](docs/reference/ARCHITECTURE.md).

## Requirements

- Home Assistant **2026.1+**.
- The **MQTT** integration configured (Zigbee2MQTT).
- Adaptive Lighting (HACS) "dummy" switches as the value source (Phase 1).

## Installation

- **HACS (custom repository):** add `https://github.com/mnestrud/light-man` as an *Integration*
  repository, install **Light Man**, restart HA.
- **Manual:** copy `custom_components/light_man/` into your HA `config/custom_components/`, restart HA.

Then **Settings → Devices & Services → Add Integration → Light Man**. It is a single-instance
integration and the setup step is a **confirm only** — there are no parameters to enter. On first run
it seeds its topology from the bundled `light_man_config.json`.

## Configuration

There is no UI options flow yet (Phase 2). The topology + per-source targets load from a Store-seeded
JSON (`custom_components/light_man/light_man_config.json`), keyed by **source**:

| Field | Meaning |
|---|---|
| `al_switch` | Adaptive Lighting dummy-switch entity (brightness/color source) |
| `consolidated_topic` | the source's group `/set` topic (steady-state flood) |
| `day_color_mode` / `night_color_mode` | `color_temp` or `rgb` |
| `night_brightness_pct` / `night_color_temp_kelvin` / `night_rgb` | Light-Man-owned night-hold target |
| `rooms.<room>.set_topic` | per-room group `/set` (addressed when the room is held) |
| `rooms.<room>.switches` | Inovelli switch base topic(s) whose actions arm/release the hold |

## Entities

- **`switch.light_man_adaptive_push`** — the single stack toggle (ON = Light Man owns the push; OFF
  restores the legacy tick automation).
- **Active-holds sensor** — count of currently-held rooms; attributes list each room and its expiry.

## Actions (services)

| Service | What it does |
|---|---|
| `light_man.release_hold` | release one room's hold (`room:` field) back to adaptive |
| `light_man.clear_holds` | release every held room |
| `light_man.force_push` | re-publish every source now, bypassing write-on-change dedup |

## Removal

**Settings → Devices & Services → Light Man → ⋮ → Delete.** On unload Light Man restores the legacy
stack (re-enables the master tick automation + the legacy enable booleans) so the house keeps adapting.
To fully remove, also delete `custom_components/light_man/` from `config/custom_components/` and restart.

## Status & quality

Phase 1 delivered and live. Quality scale: **Silver code-complete** — see
[`memory/light_man_audit.md`](memory/light_man_audit.md).

## Branch / release workflow

- `dev` — default working branch; pushed before each deploy to live HA.
- `main` — stable releases only; merged from `dev` via PR and tagged per numbered release.

## License

MIT — see [LICENSE](LICENSE).
