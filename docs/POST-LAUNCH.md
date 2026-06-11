# Light Man — Post-Launch Checklist

Standing list of deferred / put-off / explicitly-not-tackling items, carried forward after the
Phase-2 cutover (real-elevation engine is the house default; Adaptive Lighting disabled). Update the
status boxes as items are picked up. Last reviewed: **2026-06-11**.

## Pending (next, but not started)

- [x] **Release tag + version bump (0.3.0 → 0.4.0).** Phase 2 lived on `feature/adaptive-engine` only.
  — *Tagged `v0.4.0` on `dev` 2026-06-10. Repo is dev-centric: there is no `main` branch and no prior
  tags (v0.3.0 was a commit message, never tagged), so the release is tagged on `dev` by decision.*
- [x] **Inovelli `defaultLevel`/LED absorption (#8) — hardware-verified live.** — *Verified 2026-06-10
  via `force_push` MQTT trace: `defaultLevelLocal/Remote` per-source (overhead 229 / accent 76), LED-bar
  `brightness` published only for the paddle-on switch. PASS.*

## Planned next (added 2026-06-10)

- [x] **Pull out the master-switch machinery (code).** *(Done 2026-06-11, v0.5.0.)* Removed
  `_reconcile_legacy`, `_reconcile_legacy_on_start` (now `_takeover_on_start`), `async_restore_legacy`,
  `TICK_AUTOMATION`, and all `legacy_enable` reads; `async_set_push_enabled` is now a plain master on/off.
  Also dropped the vestigial AL-fallback (`al_switch`, `day/night_color_mode`, `night_*` targets,
  `sleep_switch`) and made `profile` required on every source. **Operational note:** since Light Man no
  longer disables them, the legacy tick automation + `script.al_*` must stay registry-disabled on live
  (they already are, 2026-06-10).
- [x] **Dashboard schema migration (Phase 1).** *(Done 2026-06-11, v0.6.0.)* Landed the reconciled data
  model (top-level curve library, curve-bearing source groups, first-class rooms, zones referencing room
  sensors, sleep block) in `models.py` + `light_man_config.json` (seed_version 8). `config_loader.py` derives
  the unchanged runtime view, so `coordinator.py`/`push.py` are untouched and addressing is byte-identical
  (`tests/test_seed_equivalence.py`). Next: Phase 2 (panel + read-only).
- [ ] **Web control panel (self-hosted, sidebar app).** A standalone HTML/JS app in the HA left sidebar —
  like Zigbee2MQTT / Node-RED present their own UIs — not a Lovelace dashboard. The integration registers
  a custom **panel** and serves the SPA + a backend **API** (HTTP views for Store config read/write +
  actions; websocket commands for live engine/diagnostics/occupancy/publish-log streams). Full layout
  freedom (tabs, live curves, occupancy visualizer, MQTT activity log); replaces the static
  `light_man_config.json` for day-to-day tuning and avoids the config/options flow. Full plan:
  [`docs/DASHBOARD-PLAN.md`](DASHBOARD-PLAN.md).

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
- [x] **Interim `night_brightness_pct` / `night_color_temp_kelvin` seed fields** — *(Removed 2026-06-11,
  v0.5.0)* dropped with the vestigial AL-fallback; held day/night looks are derived from the curve.

## Future tiers (not this session)

- [ ] **Gold code-complete before v1.0; Platinum ongoing.** Currently Silver.
- [ ] **Other rooms' bulb-split audit** — only the living room is confirmed (stale-NVRAM-group fix);
  other rooms unaudited.
