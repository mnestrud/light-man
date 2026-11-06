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
- **Read current state:** the retained `zigbee2mqtt/<name>` payload is normally the device's reported
  truth. Force a fresh read with `zigbee2mqtt/<name>/get` + `{"<key>":""}`.
- **Caveat — `optimistic: true`:** for devices carrying the `optimistic` option (e.g. these Hue bulbs),
  the retained payload **after a `/set` is an echo of the commanded value**, not a hardware read. To get
  true device state — notably `color_mode` under `hue_native_control` — issue a `/get` and confirm a
  *fresh* message actually arrived (`last_seen`/`linkquality` advancing, or a changed value). Trusting
  the post-`/set` echo will report "uniform" even when the bulbs disagree.

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

## Group-membership mutation — maintenance only, never runtime

```
zigbee2mqtt/bridge/request/group/members/add         {"group":"<g>","device":"<d>","endpoint":<ep>}
zigbee2mqtt/bridge/request/group/members/remove      {"group":"<g>","device":"<d>","endpoint":<ep>}
zigbee2mqtt/bridge/request/group/members/remove_all  {"device":"<d>","endpoint":<ep>}
```
- Light Man's **runtime never mutates membership** — held rooms are excluded by *addressing*, not
  membership (the rejected "dynamic group membership" approach; see `docs/PLAN.md`).
- It **is** the sanctioned **out-of-band maintenance** op for stale group state. `remove_all` sends the
  genGroups `removeAll` ZCL command, clearing the bulb's **entire NVRAM group table** — including stale
  entries Z2M's `bridge/groups` doesn't track — after which you re-add to the correct groups. Used
  2026-06-09 to fix the living-room color-mode split (bulbs had a stale group the hallway also used;
  group commands are broadcasts, so they applied the hallway rgb); the Phase 2 bulk-normalize does this
  house-wide. Detail: `docs/reference/bulb-split-investigation.md`.
- Responses land on `bridge/response/group/members/<op>` (echo `transaction` for correlation).
  `mqtt_dump.py` does **not** wrap these — send the raw `bridge/request/group/members/*` publishes.
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

## Hue native frame and measured bulb behaviour (A0, 2026-11-03)

Measured on the live house with Z2M 2.14.3, zigbee-herdsman 11.2.0, converters 26.125.0, EmberZNet
8.2.2, and the external converter `philips_datura.mjs` loaded. Raw results: `captures/a0-*` (git-ignored);
tools: `scripts/a0_bulb_test.py`, `scripts/live_probe.py`.

### Frame (cluster 0xFC03, command 0x00 `multiColor`; same format as attribute 0x0002 `state`)

`u16` LE flags, then fields in **wire order** (not bit order). Codec: `custom_components/light_man/hue_frame.py`.

| Flag | Field | Bytes | Notes |
|---|---|---|---|
| 0x0001 | on/off | u8 | |
| 0x0002 | brightness | u8 | 1..254 (Z2M clamps; 0 and 255 invalid) |
| 0x0004 | mirek | u16 | not clamped by Z2M; sent whenever `color_temp` is present |
| 0x0008 | xy | u16 ×2 | value / 65535 |
| 0x0010 | fade | u16 | deciseconds (`transition × 10`); omitted → bulb default |
| 0x0020 | effect type | u8 | not used by Light Man |
| 0x0100 | gradient colours | len + n | not used; wire position after effect type |
| 0x0080 | effect speed | u8 | not used |
| 0x0040 | gradient params | u8 ×2 | not used; last on the wire |

Bits 0x0200+ are never set (bulbs may reject unknown flags).

### Per-model results (one bulb per model, device topic)

| Model | Test bulb | Atomic set | xy ↔ CT | Staged while off | Binding ON lands on staged | Timeouts alone |
|---|---|---|---|---|---|---|
| 9290031345 LCD005 | Front Door Overhead Light 2 | pass | pass | pass | pass (first push 1.9 s) | 0 |
| 9290024687 LCA007 | Michael Closet Overhead Light | pass | pass | pass | pass (2.1 s) | 0 |
| 579573 LCD007 | Primary Bath Michael Vanity Light | pass | pass | pass | pass (2.2 s) | 0 |
| 9290024783 LTA007 | Upstairs Bath Light 1 | pass | CT-only: ignores xy | pass | pass (2.6 s) | 0 |
| 929003736601 `_01` | Hallway Light East Downlight | pass | pass | pass | pass (1.8 s) | 0 |
| 929003736601 `_02` | Hallway Light East Uplight | pass | pass | pass | pass (1.8 s) | 0 |
| 929003736701 `_01` | Office Overhead Light | pass | pass | pass | pass (1.9 s) | 0 |
| 929003736701 `_02` | Office Overhead Backlight | pass | pass | pass | pass (1.9 s) | 0 |
| 929003817002 LCL009 | Primary Bath Under Vanity Lights | pass | pass | pass | pass (2.4 s) | 0 |
| 929003151601 LCO003 | Kitchen Island Light 1 | pass | pass | pass | pass (2.1 s) | 0 |

**CT-only range (LTA007, Upstairs Bath Lights 1–3), measured 2026-11-03:** the bulb applies 222–454 mired
(≈ 4500–2200 K) and clamps anything outside it in firmware — 500 → 454, 221/200/153/100 → 222 — whether
the value arrives through Z2M's `/set` or as a raw frame. It ignores xy entirely. In the whole-house
overhead group this means the upstairs bath stays at ≈ 4500 K while the curve asks for up to 6500 K
(154 mired) at midday: a steady offset, never a jump; during a glide these bulbs plateau until the curve
re-enters their range.

"Binding ON" = the standard ZCL `genOnOff.on` an Inovelli switch sends over its binding, emulated from
the coordinator. The bulb wakes at exactly the staged brightness and mirek, so a re-push after paddle-on is
unnecessary.

### Reporting (the measurement instrument)

- Bulbs push attribute 0x0002 unprompted (Z2M → `philips_raw`). Z2M republishes the cached value on every
  state message; a report is fresh only when the value changes.
- First push ≤ 0.6 s after a change starts (carries the start/mid value); then **one push every ~10 s
  while the bulb is changing**; the settled value arrives ~10 s after a short fade. Read a settled value
  with one `/get` instead of waiting.
- RF consequence: a continuous 120 s glide makes each bulb report ~12 times per interval, versus ~2 per
  change with 1 s steps. These are acked unicasts (no broadcast-table cost).

### Fades and interruptions

- **Long fades are honoured**: 300 s, brightness 51 → 204 and mirek 400 → 300, linear within 1 point and
  1 mired at every checkpoint (LCD005, LCA007).
- **A new command interrupts from the current level**: 40 s into a 120 s fade (at 127), a 0.4 s command to 200
  went 127 → 137 → 182 → 200 in 0.6 s; the next 120 s glide started from 200 without a step.
- **Held dim** (binding `genLevelCtrl.move` / `stop`): takes over within 0.3 s, cancels the running glide, and
  the level stays put after release; the next glide starts from the dimmed level (one 2-point dip at the start).
  **Superseded 2026-11-06:** on release the VZM31-SN sets the bound bulbs to its own level (they landed on
  the switch's reported `brightness` 5 of 5 times), and a level written to the switch is forwarded to the
  bulbs within ~0.5 s. The level bindings to lights are removed; Light Man fades the lights itself (0.8.8).
  With the binding gone a level write to a switch moves only its LED bar (checked on the kitchen switch).
  **Never write a level to an off switch:** it turns the switch's own state on (the bar lights, the bulbs
  stay off) and the next paddle-up is then ignored (upstairs bath, 2026-11-06). `defaultLevelLocal` /
  `defaultLevelRemote` writes are silent in either state.
- **Z2M's cached `philips_raw`**: Z2M polls bound bulbs 1 s after a switch command and republishes the
  cached frame with the fresh `brightness`; the first message after a `/get` is therefore often stale. Use
  the `brightness` field of a `{"brightness": ""}` read, not the first frame.
- **Cross-mode / cross-hue fades are curved**: a 60 s fade from 2700 K to xy (0.30, 0.25) swung toward
  orange-red first (x 0.48 → 0.53) before curving to the target, 0.098 off the straight line — the same
  whether the start was given as mirek or as xy. The bulb chooses its own path; a straight path needs the
  engine to send short xy segments.
- **CT → xy hand-off** (2026-11-03, read-only `/get` of `color` + `color_temp` on six bulbs in CT mode at
  298–302 mired): the reported xy equals Light Man's `kelvin_to_xy` (Kang 2002) within 0.0004 (LCD005, LCA007,
  LCD007, LCO003, 929003736601/701). The first xy segment of a cross-mode blend therefore starts where the CT
  value was — no visible step at the mode switch. (Z2M may derive the CT-mode xy itself; either way it is
  the same locus the converter uses to send.)

### Consolidated flood + held-room re-assert

20 pairs of {`zgb_overhead_all` at `transition: 120`, then the held office group's look at 0.4 s, 0.5 s
later}: a **lit** held bulb deviated at most 1 brightness point and 1 mired, and settled on the look after
every pair. An **off** held bulb stages instantly, so between the flood and the re-assert it briefly holds the
flood's staged value (invisible, but the re-assert must not be lost).

### Coordinator view of broadcasts

The coordinator receives its own groupcasts back (address 0, LQI 255) and Z2M answers itself with an
`UNSUP_COMMAND` default response (local, not radio). No other device's broadcasts reach the coordinator's
application, so switch-originated groupcasts are not observable; the broadcast budget is set from BUSY
behaviour.

### Raw frame via the generic `command` converter

`/set {"command": {"cluster": "manuSpecificPhilips2", "command": "multiColor", "payload": {"data": [...]},
"options": {"disableDefaultResponse": true}}}` on a device topic applies the frame (verified by read-back).
Z2M publishes no optimistic state for it.

### Switch state write (stairwell VZM32-SN, Workstream F)

`/set {"state": "ON"|"OFF"}` on the switch topic: switch state (LED bar) changes in 0.21 s and its binding
drives the group (bulbs start within 0.45 s, 2.5 s ramp, no jump to the switch's own level with sync-level
disabled). Light Man sees the switch's state echo and reacts as to a paddle press, so its own writes must be
de-duplicated.

## Sources

- Converters: https://github.com/Koenkk/zigbee-herdsman-converters (`src/lib/philips.ts`,
  `src/lib/inovelli.ts`, `src/lib/light.ts`) — and the live `bridge/devices` `definition` they produce.
- Z2M MQTT API: https://www.zigbee2mqtt.io/guide/usage/mqtt_topics_and_messages.html and
  https://www.zigbee2mqtt.io/guide/usage/groups.html
