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

**Phase 1 scope (this deliverable):** solve the scene-reset regression by having Light Man **own the
4 per-source adaptive pushes** (tick areas a16–a19) and exclude held rooms by **addressing**
(consolidated groupcast vs. per-room groupcasts) — *not* by mutating Zigbee group membership. The tick
blueprint keeps the per-room Inovelli writes (a1–a15) and stays as an instant fallback. Later phases
replace the AL value source and migrate the rest of the tick.

> **Revision (2026-06-08):** an earlier draft of this document (and §5 below) described a **dynamic
> group-membership** mechanism with a bind/unbind primitive, transaction-correlation, a retry state
> machine, and a membership reconciler. That approach was dropped in favor of push-ownership +
> addressing. See `docs/PLAN.md` ("Design evolution") for the rationale; §5 here has been rewritten to
> match. A later simplification pass set a single solar-midnight hold TTL, memory-only dedup (no
> Store), one active-holds sensor (push-health → diagnostics), release-reuses-push, and dropped the
> `arm_hold` service.

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
requires addressing per-room (skipping held rooms) instead of flooding the consolidated group.

**Caveat:** the **hallway** source is incidentally still protected (a18/a19 share the per-room hallway
boolean). The breakage is the **overhead** source (whole main floor + bedrooms/baths/office) and
**accent**.

**Tap taxonomy (see switch-tap-instances.yaml):** double/triple paddle taps are wired to PowerView
**shades**, not lights → they must NOT arm holds.

---

## 5. The fix — design (agreed)

### 5.1 Mechanism: own the push, exclude by addressing
Light Man replaces the 4 source pushes (tick a16–a19) with its own timer-driven coordinator (own ~30 s
cadence, independent of the tick). Exclusion is an **addressing** decision, recomputed each push from
the hold set — no Zigbee topology is mutated:
- **No held room in a source** → one publish to the consolidated group `/set` (`zgb_overhead_all`,
  etc.) — byte-identical to today: same group, same RF (4 floods), full native Hue.
- **A room is held** → publish per **unheld** room to its per-room group `/set`, skipping the held one.

This works because **every bulb is in both its per-room group AND the consolidated source group**
(`z2m-groups.yaml:31-34`). Per-room groups + SBM bindings are untouched → paddle on/off keeps working
during a hold.

**Light Man owns the publish, not the groups.** `hue_native_control` is a Z2M per-group setting; Z2M
builds the native Philips `multiColor` groupcast (and the color-while-off prestage bit) on every
publish to a native group. Light Man's payload is identical to the blueprint's `mqtt.publish`
(`al-push-script-blueprint.yaml:223-230`), so native Hue is preserved on both paths — **provided every
per-room group is also `hue_native_control: true`** (almost all already are; orphans must get one — see
§5.5).

### 5.2 What this deletes vs. the membership draft
No `bridge/request/group/members/{add,remove}` primitive, no unique-per-attempt `transaction`
correlation, no 3-attempt retry state machine, no membership reconciler, no bind churn. Reliability is
recovered for free: the push is **level-triggered** (re-asserts every cycle), so a dropped flood
self-heals on the next cycle exactly as the tick does today.

### 5.3 Hold model + arm/release
- **Hold state** — `Store` (`al_held_rooms`): `room → {armed_at, expires_at}`. Intent set immediately;
  the next push honors it. Holds persist across restart (the intent must survive — else a restart
  re-clobbers a held scene). No convergence step (addressing is recomputed each push).
- **Tap seam** — Light Man **subscribes to `zigbee2mqtt/<switch>/action`** (arm/release) and to the
  Inovelli switch **state** topics (off→on release — the same room switches Light Man adapts). The
  switch-taps blueprint still applies the actual look/LED/shades; Light Man only decides whether the
  adaptive push skips that room.
- **Arm** (on `config_single`/`config_double` = Day/Night, or `up_held`/`down_held` = dim — **never**
  `*_double`/`*_triple`, which are PowerView shades): record the room held; the tap already applied the
  look. `expires_at = next solar midnight` (holds always clear overnight).
- **Release** (on `up_single` = tap-on, or room off→on via the switch state topic): clear the hold,
  then **trigger an immediate push cycle** so the room snaps to adaptive at once (reuses the push path,
  no bespoke single-room publish).
- **TTL sweep** (piggyback the push loop) clears expired holds; info line on expiry.
- **Holdable rooms** must have a per-room `set_topic` — without one the push cannot address around the
  room (it would fall back to the consolidated flood and hit the held bulbs). Validate the
  `switch → room → set_topic` chain at config load.

### 5.4 Write-on-change dedup
Keep last-published per (source, target) in memory only; skip unchanged. Replaces
`input_text.al_last_published`; `always_update=False` on the coordinator. Fold the color-mode
(rgb vs color_temp) into the dedup key so a mode flip always publishes. No `Store`: the push is
level-triggered, so the first cycle after a restart simply re-publishes once (idempotent).

### 5.5 Native-Hue coverage (the one new obligation)
Every member of `zgb_overhead_all` / `zgb_accent_all` must belong to a per-room group that is also
`hue_native_control: true` — that is the group addressed during a hold. Suspected orphans (no obvious
native per-room group): **front_door**, **michael_closet** (overhead), **under_vanity** (accent). For
each, create a `hue_native_control: true` per-room group in Z2M (one-time), else that bulb loses
color-while-off prestage while another room in its source is held. Phase-1 pre-check.

### 5.6 RF during holds
While a source has a held room, that source addresses per-room, so on each AL step the unheld rooms
get individual floods instead of one consolidated flood (~9 vs 1 for overhead). This is a transient
increase, bounded by hold lifetime; steady state (no holds) is unchanged at 4 consolidated floods.
**Accepted** — holds are transient.

---

## 6. The seam (Light Man vs. Z2M)

- **Light Man owns (Phase 1):** the 4 source pushes (a16–a19), hold state, dynamic addressing,
  write-on-change dedup, per-source day/night color mode, tap→hold arming + off→on release via MQTT
  subscribe.
- **Z2M keeps owning:** groups, membership, bindings, `hue_native_control`, Inovelli SBM, all device
  I/O. Light Man talks to Z2M **only over MQTT** (group/switch `/set` publishes + `…/action` and
  switch-state subscribes) — no `bridge/request` group mutation.
- **Switch taps (Phase 1):** Light Man **subscribes to the Inovelli action + state topics** and manages
  holds. Look application, LED effects, PowerView shade scenes, accent toggling, and the held-dim ramp
  stay in the switch-taps blueprint. The tick blueprint keeps a1–a15 (Inovelli unicast); **a16–a19 are
  disabled and owned by Light Man** — re-enabling them is the instant fallback.

---

## 7. Phases

- **Phase 0 — Bootstrap** (this repo): scaffold, CI, reference pack, CLAUDE.md, memory files.
- **Phase 1 — Own the adaptive push + scene-hold:** §5 components; Light Man owns a16–a19, tick keeps
  a1–a15. AL dummy switches stay as the value source. Deliverable.
- **Phase 2+ (roadmap, not committed):** replace the AL dummy switches with Light Man-computed
  adaptive-target profiles from **real solar elevation** (`docs/reference/adaptive-algorithm.md`) and
  remove the HACS AL integration; absorb tick a1–a15; retire the tick/push blueprints; optionally
  migrate the switch-taps blueprint's look application + held-dim ramp + occupancy. Preserve KB-encoded
  fixes as behavior + regression tests.

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
- **Zigbee RF:** multicast lost in two waves; the existing tick stack traded per-tick implicit retries
  for fewer floods, mitigated by its drift-check reconcile loop. Light Man's push is level-triggered
  (re-asserts every cycle), which provides the same self-healing without a separate reconciler.
- **Tick cadence:** AL adapts ~every 30 s; tick re-commands ~every 92 s → consumers must read
  last-published (helpers), not the live AL switch.

---

## 9. Resolved implementation decisions

1. **Tap seam** — Light Man subscribes to the Inovelli action topics (arm/release) and switch-state
   topics (off→on release).
2. **Config surface** — Phase 1 seeds a `Store`-loaded `light_man_config.json` (topology map +
   per-source color mode + per-room switches); the config-flow `user` step is a trivial confirm. The
   real OptionsFlow (adaptive-target profiles) lands in Phase 2.
3. **`integration_type`** — `hub` (local orchestrator over MQTT/Z2M; multiple sources).
4. **Push cadence** — Light Man owns its own ~30 s timer, independent of the tick (which is being
   retired), not phase-aligned to it.
5. **Hold TTL** — single rule: `expires_at = next solar midnight`.
6. **RF during holds** — accept the transient per-room-flood increase; steady state unchanged.

---

## 10. Verification

- **Unit (pytest ≥95%, 100% config_flow):** push payload + dedup, addressing selection (consolidated
  vs per-room by hold set), color-mode matrix (incl. rgb-invalid fallback), hold arm/release/TTL,
  action-string → hold mapping, off→on release, immediate-push snap on release, config-load validation
  (incl. holdable-room `set_topic` chain), services.
- **Live:** robocopy → `ha_restart` → validate. Functional (after cutover): set Day scene in a room →
  that source switches to per-room floods, the held room is skipped, scene survives ≥2 push cycles;
  tap-on (`up_single`) → instant snap + re-included; off→on → adapts; a hold with no tap-on →
  auto-expires at the next solar midnight; steady state (no holds) → exactly 4 consolidated floods.

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
| `adaptive-algorithm.md` | Phase 2 real-elevation adaptive-target engine design |
