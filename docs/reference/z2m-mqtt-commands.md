# Zigbee2MQTT command language (canonical)

Snapshot: 2026-06-09. This is the ground-truth reference for talking to the house's Zigbee devices
over MQTT (see the project rule in `CLAUDE.md` → "Device I/O"). It is sourced from the live Z2M data
(the converters' resolved output) and the converter/Z2M source — **not** guessed. For the exact,
current per-device vocabulary, ask the live data:

```
python scripts/mqtt_dump.py caps '<friendly_name>'      # settable exposes + options for that model
```

## Two layers per device — don't conflate them

Z2M's `bridge/devices` carries each device's `definition` with **both**:

| Layer | `definition.…` | What it is | MQTT mechanism |
|---|---|---|---|
| **State** | `exposes` (access `& 0b010` = settable) | live device state/attributes (`state`, `brightness`, `color_temp`, `defaultLevelLocal`, …) | `zigbee2mqtt/<name>/set` (write) · retained `zigbee2mqtt/<name>` (read) · `zigbee2mqtt/<name>/get` (on-demand read) |
| **Options** | `options` (`ea.SET` = write-only config) | converter config (`hue_native_control`, `transition`, `color_sync`, energy/power calibration, …) | `zigbee2mqtt/bridge/request/{device,group}/options` (write) · `configuration.yaml` (read — never published) |

The earlier mistake: treating `exposes` as the whole surface. **Options like `hue_native_control` are
not in `exposes`, are never published, and are set via a different topic.**

## State `/set` (devices and groups)

- **Device:** keys = that device's settable `exposes` leaves, plus the universal modifiers below.
- **Group:** keys = **union of the member devices' settable exposes**. Z2M routes a group `/set`
  through each member's converter. (`scripts/mqtt_dump.py set` validates exactly this.)
- **Universal light modifiers** (converter light extend, not per-device exposes leaves):
  - `transition` — numeric seconds.
  - `color` — composite object: `{x,y}` | `{hue,saturation}` | `{r,g,b}` | `{hex}` (allowed only when
    the target exposes a color leaf: `x`/`y`/`hue`/`saturation`).
- **Value constraints are per model** and matter — e.g. Hue `color_temp` is **153–500 mired** on most
  models but **222–454** on the filament A60, and white-only models expose **no** color. `brightness`
  is `0–254`; Inovelli `defaultLevelLocal/Remote` is `0–255`. Read them with `caps`, don't assume.
- **Read current state:** the retained `zigbee2mqtt/<name>` payload *is* the device's reported truth.
  Force a fresh read with `zigbee2mqtt/<name>/get` + `{"<key>":""}`.

## Options via the bridge API

```
zigbee2mqtt/bridge/request/group/options   {"id":"<group>","options":{...}}
zigbee2mqtt/bridge/request/device/options  {"id":"<device>","options":{...}}
```
Response on `zigbee2mqtt/bridge/response/<endpoint>`:
`{"data":{"from":{…},"to":{…},"id":…,"restart_required":<bool>},"status":"ok"|"error","error":"…"}`.
**Z2M itself validates** the option keys (returns `status:"error"`) — it is the authority, so the
helper sends and interprets the response rather than maintaining a hand-written allow-list. A
`transaction` field, if included, is echoed back for request/response correlation.

### `hue_native_control` (sourced from `zigbee-herdsman-converters/src/lib/philips.ts`)

- Defined as `exposes.Binary("hue_native_control", ea.SET, true, false)` — a **write-only converter
  option**, default `false`. It therefore never appears in `exposes` or on `bridge/groups`; read it
  from `configuration.yaml`.
- Applied to a **group** (the Light Man case) via `bridge/request/group/options`
  `{"id":"<group>","options":{"hue_native_control":true}}`.
- When enabled, the Philips converter intercepts these group `/set` keys —
  `["state","brightness","brightness_percent","color","color_temp","color_temp_percent","transition"]`
  — and emits the `manuSpecificPhilips2` `multiColor` command (one native groupcast + the
  color-while-off prestage) instead of per-bulb commands. This is the mechanism Light Man relies on
  (see `ARCHITECTURE.md` §5.1, §5.5).

## Available but intentionally NOT used by Light Man

These exist in the Z2M bridge API; Light Man avoids them by design (see `docs/PLAN.md`):

- `bridge/request/group/members/{add,remove,remove_all}` — membership mutation (the rejected
  "dynamic group membership" approach; Light Man excludes held rooms by *addressing*, not membership).
- `bridge/request/group/{add,remove,rename}` — group lifecycle. Creating a `hue_native_control:true`
  per-room group for an orphan bulb (PLAN §1.2) is a deliberate one-time manual step, not automated.

## The helper as the live, validated interface

`scripts/mqtt_dump.py` (read-only by default; writes need `--commit`):

| Command | Purpose |
|---|---|
| `caps '<name>'` | print the canonical settable exposes + options for that device's model |
| `set '<name>' '<json>' [--commit]` | state `/set`, validated vs exposes (device or group member-union); reads back & diffs the result |
| `options '<name>' [ '<json>' ] [--commit]` | read options (from `configuration.yaml`) / set via the bridge API (Z2M validates; response interpreted) |
| `sub 'zigbee2mqtt/<name>' [--seconds N]` | read current/streaming state (retained = current truth) |
| `tap [--seconds N]` | capture live switch `/action` messages |
| `bridge` | group membership + device count |

## Sources

- Converters: https://github.com/Koenkk/zigbee-herdsman-converters (`src/lib/philips.ts`,
  `src/lib/inovelli.ts`, `src/lib/light.ts`) — and the live `bridge/devices` `definition` they produce.
- Z2M MQTT API: https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html and
  https://www.zigbee2mqtt.io/guide/usage/groups.html
