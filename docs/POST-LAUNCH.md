# Light Man — Post-Launch Checklist

Standing list of deferred / put-off / explicitly-not-tackling items, carried forward after the
Phase-2 cutover (real-elevation engine is the house default; Adaptive Lighting disabled). Update the
status boxes as items are picked up. Last reviewed: **2026-06-10**.

## Pending (next, but not started)

- [x] **Release tag + version bump (0.3.0 → 0.4.0).** Phase 2 lived on `feature/adaptive-engine` only.
  — *Tagged `v0.4.0` on `dev` 2026-06-10. Repo is dev-centric: there is no `main` branch and no prior
  tags (v0.3.0 was a commit message, never tagged), so the release is tagged on `dev` by decision.*
- [x] **Inovelli `defaultLevel`/LED absorption (#8) — hardware-verified live.** — *Verified 2026-06-10
  via `force_push` MQTT trace: `defaultLevelLocal/Remote` per-source (overhead 229 / accent 76), LED-bar
  `brightness` published only for the paddle-on switch. PASS.*

## Deferred to the user, by their call

- [ ] **East mmwave detection-region tuning.** The East sensor doesn't detect descent, so no
  `occupancy` edge is published and Light Man (correctly) does nothing. Z2M-side device setting, **not a
  Light Man code path**. User adjusting sensor dimensions separately/later.
- [ ] **West sensor hallway-sweep direct capture.** Its directional order (west→center→east) was masked
  by write-on-change dedup (hallway already lit by the East sweep). Same verified code path as East —
  optional to capture cleanly later (trigger the west sensor with the hallway dark).
- [ ] **HA-config blueprint + 3 occupancy automations — commit the gating edits.** They are gated off
  but the edits remain **uncommitted in `ha_vibecode_git`** (user chose not to commit). Reversible by
  toggling `push_enable` OFF.

## Decided to leave as-is (not absorb)

- [ ] **Blueprint keeps the hardware held-dim binding** — explicit call: "let the hardware binding be in
  charge of ramping." Blueprint also keeps accent, shades, and LED/notifications.
- [ ] **Other-room occupancy** — only hallway + stairwell were absorbed; the rest stay legacy.
- [ ] **`under_vanity` accent** — no paddle, so off-respect can't skip it (known minor, accepted).
- [ ] **Per-room addressing + space publishes** — the *old* always-per-room + inter-publish-spacing
  variant was reverted (v0.2.3 on main) and is not being revisited. (Note: the *current* conditional
  per-room addressing — consolidated when uniform, per-room on hold/divergence — **is** in use; this
  item is only about the abandoned older variant.)
- [ ] **Interim `night_brightness_pct` / `night_color_temp_kelvin` seed fields** — superseded by the
  engine, left in place harmlessly (still used by the fallback path for a profile-less source).

## Future tiers (not this session)

- [ ] **Gold code-complete before v1.0; Platinum ongoing.** Currently Silver.
- [ ] **Other rooms' bulb-split audit** — only the living room is confirmed (stale-NVRAM-group fix);
  other rooms unaudited.
