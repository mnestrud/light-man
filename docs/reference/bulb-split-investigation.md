# Living-Room Bulb Color-Mode Split — Investigation Log

**Status:** RESOLVED (2026-06-09). Root cause: **stale Zigbee group membership in the
firmware NVRAM of 3 living-room bulbs (LR1/3/5)** — a group ID also used by the hallway,
which Z2M's `bridge/groups` did not reflect. Group commands are broadcasts (multicast →
NWK broadcast), so the hallway's `color` group broadcast was legitimately delivered to
those 3 bulbs because the silicon considered them members. Fix: `bridge/request/group/
members/remove_all` on each (clears the entire NVRAM group table), then re-add to only
`zgb_living_room` + `zgb_overhead_all`. Verified: the hallway rgb alone, and the full
Light Man flood with rgb included, now leave all 6 bulbs uniform color_temp. NOT spacing,
NOT the converter, NOT RF.

The v0.2.3 spacing/ordering change was built on a (disproven) broadcast-pileup theory and
did **not** fix this — even an isolated, last-fired, 1s-deferred hallway rgb still split
the same 3 bulbs. It is optional mesh hygiene, not the fix.

**Date:** 2026-06-09
**Symptom:** When Light Man owns the push, the 6 identical Hue bulbs in
`zgb_living_room` split — some land in `xy` color mode at a color never sent to
their group, while the rest stay `color_temp`. Legacy stack never does this.

---

## Environment / ground-truth facts

- 6 living-room bulbs (group `zgb_living_room`, id 1): ieee
  `0x001788010d6e23ab, …de6acdb, …d487fff, …de6ad1f, …d6e1ff4, …d6e0c6b`.
  Also members of `zgb_overhead_all` (id 30, 28 members).
- Every bulb option (Z2M `configuration.yaml`): `optimistic: true`,
  `hue_native_control: true`, `color_sync: false`, `state_action: false`,
  `transition: 0.4`. Group `zgb_living_room`: `hue_native_control: true`.
- **`optimistic: true`** ⇒ post-`/set` state is an echo of the *commanded* value,
  NOT a device read. Trustworthy reads require an explicit `/get`
  (`readAttributes`), confirmed fresh via a new message arriving.
- Bulbs are in Smart Bulb Mode, bound to the Inovelli paddle via direct Zigbee
  binding (bypasses Z2M ⇒ Z2M's retained bulb `state` can be stale; the **switch**
  is canonical on/off).
- `last_seen` is not enabled in this Z2M ⇒ cannot use it for freshness; use
  "a fresh message arrived after `/get`" instead.

## Payload comparison (static, from code)

Legacy push (`AL_MQTT_script_blueprint.yaml`, via `script.al_overhead_source` →
`zgb_overhead_all`, and per-room `al_*` scripts):
`{"brightness": <0-254>, "color_temp": <153-500>, "transition": <s>}` — qos 0,
retain false. Legacy spaces publishes: tick `per_area_delay: 4s`, script
`inovelli_group_delay: 0.5s`.

Light Man push (`push.py build_payload` → `coordinator._mqtt_publish`):
`{"brightness": <0-254>, "transition": <s>, "color_temp": <153-500>}` — qos 0,
retain false (HA `mqtt.async_publish` defaults). **No inter-publish delay.**

Differences: (a) key order (`transition` vs `color_temp` position), (b) transition
value (LM 1.0 vs legacy 5.0). Both **disproven** as the cause — see tests.

---

## Tests run

| # | Hypothesis | Method | Result | Conclusion |
|---|---|---|---|---|
| 1 | Key order causes split | A/B: LM-order vs legacy-order single `/set` to `zgb_living_room`, transition held 1.0, `/get` readback | Both uniform `color_temp` on all 6 | **Key order NOT the cause** (later flagged: reads were optimistic-suspect; re-confirmed in #4/#7) |
| 2 | optimistic echo masking truth | Read Z2M options | `optimistic: true` on all bulbs | Post-`/set` state is echo; must use `/get` |
| 3 | bulbs off | read `state` from bulb topic | stale/None (binding bypasses Z2M) | Use **switch** as canonical on/off |
| 4 | (clean A/B) key order, tick OFF | Disabled master tick; LM-order single `/set` ct250 + genuine `/get` (fresh=True) | all 6 `color_temp` 250, uniform; **user confirmed visually identical** | Single LM-order command = uniform |
| 5 | (clean A/B) legacy order | legacy-order single `/set` ct400 + `/get` | all 6 `color_temp` 400, uniform | Single legacy-order command = uniform |
| 6 | reproduce real failure | Flipped `push_enable` ON (tick off), captured live | **SPLIT captured:** LR1/3/5 `xy (0.52518,0.38817)`, LR2/4/6 `color_temp` ct370; brightness 77 uniform | Split is real; under live Light Man |
| 6b | what did LM send | sniff `/set` + `light_man.force_push` | **14 group publishes, same instant 18:01:56**; 13× `color_temp 370`, 1× `zgb_hallway_up` `color rgb(255,167,87)`. LR group got `color_temp 370` (correct) yet 3 bulbs went xy to a color never sent to their group | Split is a **burst** effect, not the per-group command |
| 7 | spacing of color_temp burst | 4 color_temp groups (LR, kitchen, bedroom, office) BURST (0-delay) vs SPACED (0.5s); `/get` LR | Both **uniform** | **Spacing NOT the cause; color_temp-only burst is clean** |
| 8 | LM/my comms differ from user | sniff user's `mqtt.publish` | `{"brightness":90,"transition":1.0,"color_temp":250/500}` qos0 retain0 — **byte-identical** to LM and my paho | Comms method NOT the cause |
| 9 | single command via my method splits | reissue user's exact `ct500` single `/set` via paho + `/get` | all 6 uniform `color_temp` 500 | Single command never splits (any sender) |
| 10 | intermittent single-command split | 20× single `/set` (alternating ct260/410) + `/get` each | **0/20 splits** | Single commands bulletproof |

---

## Where it stands

- **Cleared (not the cause):** key order, transition value, spacing of
  color_temp commands, MQTT comms method (paho vs HA `mqtt.publish`), single
  commands in general, optimistic-echo artifact (reads validated via `/get` +
  user's eyes).
- **Confirmed:** the split occurs **only** under Light Man's burst — 14 group
  `/set`s fired in the same millisecond, one of which carries `color` (rgb), the
  rest `color_temp`. A color_temp-only burst of 4 groups did NOT split.
- **Open discriminators (untested):**
  1. presence of a concurrent `color`/rgb publish in the burst (only the full
     LM burst had one; the clean color_temp burst did not), vs
  2. group count (14 vs the 4 tested).

## Code audit (push path) — 2026-06-09

Traced: `coordinator._run_push / _push_source / _publish / _mqtt_publish /
_read_adaptive`, `push.build_payload / build_night_payload / plan_publishes /
_room_target / resolve_color_mode`, `config_loader`, `models`.

- **No payload bug.** `build_payload` returns a fresh dict per call; never mutated
  after; no shared mutable defaults. No color-mode bleed across sources
  (`resolve_color_mode(overhead)` = color_temp; rgb only resolves for hallway
  sources). `plan_publishes` for a sourced source returns ONLY per-room topics —
  never also `zgb_overhead_all` — so living-room bulbs receive only
  `zgb_living_room` from LM. The captured live bytes confirm: living room got
  `color_temp 370` (correct).
- **The one real code difference vs legacy:** Light Man has **zero inter-publish
  spacing.** `_run_push` loops sources and `_push_source` loops topics calling
  `mqtt.async_publish` back-to-back with no `await asyncio.sleep` — all 14 groups
  fire in the same millisecond. Legacy spaces every publish by design
  (`per_area_delay: 4s` in tick blueprint, `inovelli_group_delay: 0.5s` in
  `AL_MQTT_script_blueprint`).
- **Caveat:** test #7 (color_temp-only, 4 groups) was clean spaced or not — but it
  had no rgb. The failing burst had one rgb command. So "no spacing" is the code
  defect; the rgb-in-burst is the untested case it likely bites.

## DECISIVE TEST #11 — remove the rgb from the flood (2026-06-09)

Reframing (from user): the split color `(0.52518,0.38817)` == `rgb(255,167,87)` computed
via Z2M's `rgbToXY` (delta 0.00002/0.00004 — exact). But that rgb IS just the current
adaptive value; `hallway_up`'s AL source switch has the same light curve as the
color_temp sources, so the value matching the hallway is meaningless. The real defect:
the **`color` (rgb) publish in the flood** flips other bulbs to xy mode.

Test: flipped `hallway_up.day_color_mode` rgb→color_temp (bundled JSON + live Store
`.storage/light_man_config`, then reloaded the config entry — no OptionsFlow exists yet,
so the Store was the only live path). Disabled tick, flipped `push_enable` ON, captured
the flood + `/get` living room.

- Flood now: `zgb_hallway_up/set {brightness:76, transition:1.0, color_temp:370}` — NO rgb.
- Living room: **all 6 `color_temp` 370, uniform. NO SPLIT.**

**CONFIRMED ROOT CAUSE:** a concurrent `color`/rgb hue_native publish in Light Man's
zero-delay flood corrupts other overhead bulbs into xy mode. No rgb in the flood ⇒ clean.

**Caveat / not yet fixed:** `hallway_up.night_color_mode` is still `rgb` (and
`hallway_down.night_color_mode` is `rgb`) — so the split returns at night unless the fix
handles the rgb case itself rather than avoiding it. Open fix decision: (a) keep hallway
color_temp (loses the rgb look), or (b) make Light Man emit/se­quence the rgb publish so
it can't corrupt other groups.

## MECHANISM (source-grounded) — 2026-06-09

- **zigbee-herdsman `Group.command`** → `adapter.sendZclFrameToGroup(groupID, frame)`.
- **ember adapter** sends it as `EmberOutgoingMessageType.MULTICAST`, `destinationEndpoint: 0xff`.
  A Zigbee MULTICAST is a **NWK broadcast** — it floods the whole mesh; *every* bulb
  receives the frame. Group membership is a receiver-side **filter**, not addressing.
- **philips.ts `EncodeManuSpecificPhilips2`**: `color_temp` sets only `ColorMirek (0x0004)`
  (no xy carried) → a stray apply is invisible. `color` sets `ColorXY (0x0008)` +
  `color_mode="xy"` → a stray apply visibly flips the bulb to xy with that color.

End-to-end: LM fires 14 group commands with no spacing → 14 broadcasts hit the mesh at
once → every living-room bulb receives the hallway `color` broadcast → under the flood,
membership filtering doesn't hold on some bulbs and they apply it → 3 LR bulbs flip to xy
at the hallway's exact color. The 12 color_temp broadcasts mis-apply invisibly. Legacy
spaces publishes (4s/0.5s) so broadcasts never pile up.

Proven: group=broadcast (herdsman), color carries xy/color_temp doesn't (philips.ts),
removing rgb from the flood → clean (test #11). Inferred: exact reason non-members act
under load (broadcast congestion). Fix direction: space publishes and/or never emit a bare
`color` broadcast in the flood.

## FIX (v0.2.3) — space publishes + order color last

`_run_push` now gathers all sources into one flat plan, applies `push.order_plan`
(color_temp publishes first, bare `color`/xy publishes last), and publishes via
`_publish_spaced`: `INTER_PUBLISH_DELAY_S` (0.3s) between real sends, `RGB_DEFER_S`
(1.0s) before the first `color` broadcast. Spacing keeps the per-group Zigbee
multicasts from piling on the mesh; the color-last ordering + larger gap keeps the
xy broadcast out of the color_temp flood so it can't flip non-member bulbs. Dedup
no-ops don't pace (gap only follows a real publish). Mirrors legacy's spacing
(`per_area_delay`/`inovelli_group_delay`). 80 tests / 100% cov / mypy clean.

## Test methodology notes (for reproducibility)

- Creds via `scripts/mqtt_dump.py` `resolve_connection` (HA `.storage`), host
  `botworth`. Hardcoded `mqtt-user/mqtt-user` is WRONG → `Not authorized`.
- Genuine device read: publish `<bulb>/get` `{"color":"","color_temp":""}`, then
  read the resulting `zigbee2mqtt/<bulb>` message; confirm a fresh message arrived.
- Master tick `automation.ataraxia_lighting_master_tick_automation` disabled during
  tests to stop it squashing publishes; **must be re-enabled** when done.
- `push_enable` = `switch.light_man_adaptive_push`. Currently **ON** (Light Man
  owns) with tick OFF — restore by turning it OFF (re-enables tick + booleans).
