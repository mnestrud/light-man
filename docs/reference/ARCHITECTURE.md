# Light Man — Architecture & Design Reference

Snapshot date: 2026-06-06. This document is the ground-truth context for the implementation plan
(refined remotely via Ultraplan). It captures the existing "Ataraxia Adaptive Lighting" YAML stack,
the regression Light Man must fix, and the agreed design.

---

## 1. Purpose

Light Man is a Home Assistant custom integration that orchestrates adaptive lighting (AL) across a
Zigbee2MQTT + Philips Hue + Inovelli Blue house. It is the Python successor to a large blueprint stack
that has outgrown YAML/Jinja (a string of subtle bugs: the "LR latch", a tick split mid-fan-out, a
hallway drift, a keyed-JSON state migration, and now the scene-reset regression below).

**Phase 1 scope (this deliverable):** solve the scene-reset regression via **dynamic Zigbee group
membership** — without changing the existing tick. Later phases migrate the tick loop itself.

---

## 2. System context

- **Devices:** ~15 Inovelli VZM31-SN switches + 3 VZM32-SN mmWave switches; a large Hue bulb fleet;
  SmartThings leak sensors; Third Reality soil/buttons/night-light. Z2M on channel 26 (ember/EZSP).
- **Adaptive values** come from HACS Adaptive Lighting "dummy" switches used purely as value sources
  (`brightness_pct`, `color_temp_kelvin`): one global overhead source switch, plus accent and hallway
  up/down source switches.
- **The Master Tick** (`automation 1758389535606`, blueprint `tick-blueprint.yaml`) is a timer-driven
  fan-out over 19 "areas" (a1–a19), gated on the Z2M bridge being online, with a rest-period model
  (~92 s cycle: ~4 s per-area stagger + 20 s rest). Each area reads AL values and calls a per-area push
  script (`al-push-script-blueprint.yaml`).
- **The push script** publishes via MQTT: to the Inovelli switch (`defaultLevelLocal/Remote` for
  tap-on brightness, plus a `brightness` LED-bar write while the switch is on) and/or to a Z2M light
  **group** as a STATELESS native `multiColor` (brightness + color_temp, no on/off bit). rc29 removed
  all `state` writes (latch fix); rc30 added write-on-change dedup via `bri_changed`/`kel_changed`
  flags from the tick.
- **On/off is owned by the Inovelli Smart Bulb Mode (SBM) binding** to the per-room group, never by the
  AL flood. Color is staged-while-off through Hue `hue_native_control` (the `multiColor` bitmask sets a
  persistent prestage bit so off bulbs show the right color on turn-on).

---

## 3. The consolidation (the refactor that introduced the bug)

To cut RF (Zigbee multicast is lossy in two waves; see KB), the per-room bulb pushes were collapsed
into **4 consolidated per-source group floods**:

| Tick area | Script | Group (`/set`) | Enable boolean (gate) |
|-----------|--------|----------------|------------------------|
| a16 overhead | `al_overhead_source` | `zgb_overhead_all` (24+ bulbs, 10 rooms) | `input_boolean.adaptive_lighting_overhead_all` |
| a17 accent | `al_accent_source` | `zgb_accent_all` | `input_boolean.adaptive_lighting_accent_all` |
| a18 hallup | `al_hallup_source` | `zgb_hallway_up` | `input_boolean.adaptive_lighting_switch_hallway` |
| a19 halldown | `al_halldown_source` | `zgb_hallway_down` | `input_boolean.adaptive_lighting_switch_hallway` |

The 10 per-room overhead scripts (a1–a15) had their `group_set_topic` **blanked** → they now drive
**only the Inovelli switch** (defaultLevel/LED bar). Bulb color/brightness comes **solely** from
a16–a19. This dropped ~11 group floods/tick to 4.

---

## 4. The confirmed regression

**Symptom:** a per-room manual *light* look (Config Day/Night, or Held-dim) is overwritten by the next
tick (~92 s), and there is no per-room way to prevent it.

**Evidence chain:**
1. The per-room enable booleans the switch taps toggle (`adaptive_lighting_switch_<room>`, = a1–a15
   enables) no longer gate any bulb push — only the per-room Inovelli unicast (group push blanked).
2. Bulb color/brightness is driven by a16–a19, gated by **global** source booleans
   (`adaptive_lighting_overhead_all`, `adaptive_lighting_accent_all`). Turning off a room's helper does
   not stop the overhead flood from re-asserting AL to that room's bulbs.
3. The switch-tap blueprint's Config Day/Night and Held-dim turn the per-room helper OFF — which used
   to matter pre-consolidation but is now inert for bulbs.
4. rc30 dedup gives **no** protection: `bri_changed`/`kel_changed` compare the new AL target to the
   last *AL* target (helper `input_text.al_last_published`), not to the bulbs' scene state. The flood
   fires whenever AL drifts one step (~30 s), clobbering the scene.
5. The only "off switch" is the global source boolean — which freezes AL for all 10 overhead rooms.

**Root cause (architectural):** override is a per-room/per-light concept, but adaptation is now a
per-source group **multicast** that cannot exclude individual members. Restoring per-room exclusion
requires removing a held room's bulbs from the consolidated group.

**Caveat:** the **hallway** source is incidentally still protected (a18/a19 share the per-room hallway
boolean). The breakage is the **overhead** source (whole main floor + bedrooms/baths/office) and
**accent**.

**Tap taxonomy (see switch-tap-instances.yaml):** double/triple paddle taps are wired to PowerView
**shades**, not lights → they must NOT arm holds.

---

## 5. The fix — design (agreed)

### 5.1 Governing principle: intent vs. convergence
- **Intent** lives in a `Store`-persisted helper (`al_held_rooms`) — the set of rooms excluded from the
  flood. Set immediately on arm/release, regardless of command success.
- **Convergence** is the reconciler's job: make group membership match intent.
- Immediate response-checked retries only *shorten* convergence; if they all fail, the reconciler still
  fixes it. This split is what makes mutating live Zigbee topology safe.

### 5.2 Mechanism: dynamic group membership
A Zigbee group is a 16-bit id in each bulb's group table; the AL flood is an APS groupcast only acted
on by members. Remove a held room's bulbs from `zgb_overhead_all` (via Z2M, so `hue_native_control`
rebuilds its `multiColor` payload for the smaller set) and the flood passes them by. Per-room group
membership + SBM binding are untouched → paddle on/off still works during a hold.
- **Steady state: zero added RF** (still 4 floods/tick). Cost is only a small unicast burst per
  arm/release (one Add/Remove-Group per bulb, ~2–6 per room).

### 5.3 MQTT bridge primitive + retry state machine
Use Z2M `bridge/request/group/members/{add,remove}` with a unique-per-attempt `transaction`; await the
matching `bridge/response/...` (`status: ok|error`, echoes `transaction`). `ok` means the bulb
acknowledged the command (Z2M awaits the device response). Verify the exact payload/topic live.

Per bulb (immediate path):
```
attempt = 1
loop:
  txn = "<room>-<bulb>-<run_ts>-<attempt>"     # unique PER attempt (avoid stale-response aliasing)
  publish(request, transaction=txn)
  wait_for response(transaction==txn), timeout=5s, continue_on_timeout=true
  ok      -> done
  error/timeout -> if attempt>=3: record failure; else backoff(0.5s,1.5s); attempt++
```
- Add/Remove are **idempotent** → a false-timeout retry is harmless.
- We have a ~90 s budget before the next flood, so this runs in a **dedicated script/coroutine**
  (`mode: queued`), never blocking the tap; the user never waits.

### 5.4 Arm / release
- **Arm** (on Config Day/Night or Held-dim for a room — never double/triple): set intent; remove the
  room's bulbs from its source group (3-attempt). The tap already applied the look.
- **Release triggers (two):**
  1. **Up-single ("tap on")** — immediate: clear intent, re-add bulbs, and **one-shot push current AL**
     to the room's per-room group so it snaps to adaptive instantly (don't wait up to ~90 s for the
     next flood — the per-room scripts have group blanked, so re-adding alone won't repaint).
  2. **Off→on** — turning the room off (RESET_LED) clears the hold and re-adds bulbs; also restage AL
     color while off so the next turn-on is adaptive (not the lingering scene color).

### 5.5 Reconciler (the actual guarantee)
- Desired = overhead/accent bulbs whose room ∉ `al_held_rooms`; actual = read from Z2M groups; issue
  the diff (same response-checked publish).
- **One attempt per bulb per run** (the run cadence IS the cross-run retry; level-triggered, so it
  re-tries every run until reachable). Runs on HA start, every 2nd tick (piggyback the existing
  drift-check), and once after any partial-failure arm/release.

### 5.6 Failure notification
- One shared "failing bulbs" set; whoever first detects a failure (immediate path or reconciler) adds
  the bulb and emits **one** warning. No per-run spam (a bulb off at the wall is benign and converges
  on power-up).
- `system_log.write` `level: warning`, `logger: light_man.hold`. Message names room, bulb, op, last
  status. **One ~1 h escalation** if still failing; info line on recovery; silent retry between.

---

## 6. The seam (Light Man vs. Z2M)

- **Light Man owns (Phase 1):** hold state, dynamic membership, reconciler, retry, warnings.
- **Z2M keeps owning:** groups, bindings, `hue_native_control`, Inovelli SBM, all device I/O. Light Man
  talks to Z2M **only over MQTT** (`bridge/request|response` + group `/set`).
- **Switch taps (Phase 1):** the existing switch-tap blueprint calls a Light Man service
  (`light_man.arm_hold` / `release_hold`), or Light Man subscribes to the Inovelli action topics. LED +
  shade actions stay in the blueprint. **The existing tick blueprint is unchanged in Phase 1** — it
  floods whatever is in the group; membership does the exclusion.

---

## 7. Phases

- **Phase 0 — Bootstrap** (this repo): scaffold, CI, reference pack, CLAUDE.md, memory files.
- **Phase 1 — Scene-hold fix:** §5 components; tick untouched. Deliverable.
- **Phase 2+ (roadmap, not committed):** migrate the tick fan-out + write-on-change dedup (replace
  `input_text.al_last_published`) + drift-check + per-source pushes into the coordinator; retire the
  tick/push blueprints; optionally migrate tap handling/occupancy. Preserve KB-encoded fixes as
  behavior + regression tests.

---

## 8. Relevant KB facts (evidence-tracked)

- **LR latch (fixed):** a group's any-member-on plus a `state:ON` re-assert latched lights on for
  hours. Fix: AL never sends `state` — stateless `multiColor` only; on/off owned by the switch binding.
- **Hue native control:** consolidated groups run `hue_native_control: true`; color stages while off
  via the `multiColor` bitmask (write-only Philips cluster). Trust **brightness** reports for
  verification; color HA-state can be an optimistic echo. Mixed-bulb groups clamp HA color range to the
  narrowest member, but the MQTT path clamps per-bulb.
- **Inovelli SBM binding:** VZM31-SN binds from EP1; VZM32-SN (mmWave) binds from **EP2**.
  `BindingOffToOnSyncLevel: Enabled` on all switches → tap-on uses `defaultLevelLocal` written each
  tick.
- **Zigbee RF:** multicast lost in two waves; consolidation traded per-tick implicit retries for fewer
  floods, mitigated by the drift-check reconcile loop.
- **Tick cadence:** AL adapts ~every 30 s; tick re-commands ~every 92 s → consumers must read
  last-published (helpers), not the live AL switch.

---

## 9. Open implementation decisions (resolve during build)

1. Tap seam: Light Man subscribes to Inovelli action topics vs. blueprint calls the service.
2. Config surface for the area/room ↔ bulb ↔ group/topic mapping (config entry + options vs. imported
   YAML map).
3. Confirm `integration_type` `hub` vs `service` (HA `ha-dev` check).

---

## 10. Verification

- **Unit (pytest ≥95%, 100% config_flow):** retry state machine (ok/error/timeout/idempotent re-issue),
  reconciler diff/convergence, notification transition/suppression/escalation, hold arm/release.
- **Live:** robocopy → `ha_restart` → validate. Functional: set Day scene in a room → bulbs leave
  `zgb_overhead_all` and the scene survives ≥2 ticks; tap-on → instant snap + re-added; off→on →
  adapts; kill a bulb mid-hold → exactly one warning + reconciler converges on power-up.

---

## Reference files in this folder

| File | What |
|------|------|
| `tick-blueprint.yaml` | Master Tick blueprint (v1.0rc10) |
| `switch-taps-blueprint.yaml` | Inovelli Switch Taps blueprint (v1.00rc9) |
| `al-push-script-blueprint.yaml` | AL push script blueprint (v1.0rc30) |
| `tick-automation-instance.yaml` | Live a1–a19 wiring |
| `al-source-scripts.yaml` | 4 source scripts + a per-room script (blanked group) |
| `switch-tap-instances.yaml` | Tap instances — double/triple = PowerView shades |
| `z2m-groups.yaml` | Group definitions + reconstructed membership |
