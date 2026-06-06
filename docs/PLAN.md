# Plan — Ataraxia Adaptive Lighting integration + scene-hold fix

## Context

The recent AL refactor (Stage 0/1/2b consolidation) collapsed per-room AL pushes into **4 per-source
group floods** (tick areas a16–a19) that drive every bulb via native `multiColor` groupcasts to
consolidated Z2M groups (`zgb_overhead_all`, `zgb_accent_all`, `zgb_hallway_up/down`), each gated only
by a **global** source-enable boolean.

**Regression confirmed (evidence-traced):** a per-room manual *light* look can no longer be protected
from the next tick.
- Per-room scripts (e.g. `al_living_room_recessed`) have `group_set_topic: ""` blanked → they drive
  only the Inovelli switch (defaultLevel/LED), never bulbs.
- Bulb color/brightness now comes solely from a16–a19, gated by `adaptive_lighting_overhead_all` /
  `adaptive_lighting_accent_all` (hallway shares its per-room boolean, so it's incidentally still
  protected). The per-room enable (`adaptive_lighting_switch_<room>`) the switch taps toggle no longer
  gates any bulb push.
- So Config Day/Night (config button) and Held-dim set a look, but the next flood (~92 s) overwrites
  it. rc30 dedup gives **no** protection — it compares the new AL target vs the last *AL* target, not
  vs the bulbs' scene state.
- Double/triple paddle taps are wired to **PowerView shade scenes** (`scene.powerview_hub_1_*`), not
  lights → they must **not** arm holds.

A correct fix needs per-room exclusion from a group multicast → dynamic Z2M group membership + a
hold-state model + response-checked retry + a reconciler + rate-limited core-log warnings. That logic
is stateful/async/error-heavy — the most fragile YAML yet if done in blueprints, and the latest in a
series of subtle bugs that show this stack has outgrown YAML/Jinja.

**Decision:** build a custom HA integration following the particle-man gold standard, and deliver the
scene-hold fix as its first feature — implemented as **group-membership management only**, so the
existing tick blueprint keeps running untouched during Phase 1. Migrate the rest of the AL stack into
the integration in later phases.

## Integration identity (template placeholders — `ha-integration-CLAUDE.md`)

| Placeholder | Value |
|---|---|
| INTEGRATION_NAME | Light Man |
| DOMAIN | `light_man` *(core `light` is reserved → `light_man`)* |
| REPO_URL | https://github.com/mnestrud/light-man (**public**) |
| REPO_LOCAL_PATH | `C:\Users\micha\code\light-man` |
| CURRENT_QUALITY_TIER | pre-bronze (target Silver before first internal release) |
| AUDIT_MEMORY_FILE | `light_man_audit.md` |
| PLATFORMS | `sensor`, `switch` (+ services) |
| HA_MIN_VERSION | match current live (2026.x) |
| integration_type | `hub` (local orchestrator over MQTT/Z2M) — verify via `ha-dev` |
| iot_class | `local_push` |
| License | MIT |

Public repo — keep the full particle-man structure/test/CI rigor (brand/store assets optional since
it's a personal orchestrator, not HACS-store-targeted).

**Locked design decisions (cemented — avoid remaking):**
- **Domain `light_man`** — baked into folder path, `manifest.json`, every entity `unique_id`,
  config-flow handler, service names (`light_man.arm_hold`), and translation keys.
- **Entity `unique_id` scheme:** `{entry_id}_{room}_{kind}` (e.g. `…_living_room_hold`). Changing later
  orphans registry entries (lost history / customizations / dashboard refs).
- **Config model:** UI config-flow, **single config entry** (`single_config_entry: true`) — house-wide
  singleton, not multi-instance.
- **Manifest:** `integration_type: hub`, `iot_class: local_push`, `dependencies: ["mqtt"]`.

**Branch / release workflow:**
- `dev` = default working branch; **each robocopy deploy is preceded by a `git push` to `dev`**.
- `main` = stable releases only; **merge `dev`→`main` and tag a numbered release once stable**.

## Architecture & seam

- **Integration owns (Phase 1):** hold state, dynamic group membership, reconciler, retry, warnings.
  **Later:** tick loop, write-on-change dedup, drift-check, per-source pushes, tap→hold arming.
- **Z2M keeps owning device I/O:** groups, bindings, `hue_native_control`, Inovelli SBM. The
  integration talks to Z2M **only via MQTT** — `bridge/request|response/...` + group `/set` topics.
- **Switch taps (Phase 1):** the existing switch-tap blueprint calls an integration service
  (`light_man.arm_hold` / `release_hold`) — or the integration subscribes to the Inovelli action
  topics. LED + shade actions stay in the blueprint. Final seam decided in build.

## Phase 0 — Bootstrap (per `integration_dev.md`)

1. **Public** GitHub repo `light-man` (MIT `LICENSE`), branches `dev` (default) + `main`; copy
   `.github/workflows/` (validate, claude-code-review, claude, docs) from particle-man.
2. Template → `CLAUDE.md` (fill placeholders). Create `light_man_audit.md` (reset to TODO/FAIL) +
   `light_man_test_env.md`. Add a `## Integration: Light Man` section to MEMORY.md.
3. Standard structure: `custom_components/light_man/` (`__init__`, `coordinator`, `config_flow`,
   `const`, `sensor`, `switch`, `manifest`, `strings.json`, `translations/en.json`, `icons.json`,
   `py.typed`, `diagnostics`), `tests/`, `pyproject.toml`, `requirements_test.txt`, `hacs.json`.
4. **Reference pack for Ultraplan** — create `docs/reference/` and stage the current AL YAML + context
   so the cloud session has full ground truth (no Samba/`.claude` access in cloud):
   - `tick-blueprint.yaml` (Tick Blueprint v1.0rc10)
   - `switch-taps-blueprint.yaml` (Inovelli Switch Taps v1.00rc9)
   - `al-push-script-blueprint.yaml` (AL_MQTT_script_blueprint v1.0rc30)
   - `tick-automation-instance.yaml` (live a1–a19 wiring, automation `1758389535606`)
   - `al-scripts.yaml` (the 4 source scripts + a representative per-room script showing blanked
     `group_set_topic`)
   - `switch-tap-instances.yaml` (automations.yaml excerpt — shows double/triple = PowerView shades)
   - `z2m-groups.yaml` (`zgb_overhead_all` / `zgb_accent_all` membership from
     `zigbee2mqtt/configuration.yaml`)
   - `ARCHITECTURE.md` — consolidation summary, the confirmed regression, the membership/hold/
     reconciler/retry fix design, and relevant KB facts (LR latch, hue native control, SBM binding,
     RF two-wave loss).
5. Windows PHCC: venv + copy `sitecustomize.py` + pin PHCC/mypy.
6. Commit to `dev`, push; **re-launch Ultraplan scoped to `C:\Users\micha\code\light-man`** for remote
   plan refinement.

## Phase 1 — Scene-hold fix (deliverable)

**Pre-checks (read-only, live):** confirm Z2M `bridge/response/group/members/{add,remove}` topic +
payload shape + `transaction` echo; confirm every overhead/accent bulb is **also** in a per-room group
(needed for scene apply + one-shot AL re-push on resume); enumerate per-room bulb IEEE+endpoint
membership of `zgb_overhead_all` / `zgb_accent_all`.

**Components:**
- **Hold model** — per-room hold state in memory, persisted via `Store` (`al_held_rooms`).
  Intent-vs-convergence split: intent set immediately; convergence is the reconciler's job.
- **MQTT primitive** `async_set_membership(bulb, group, add|remove)` — publish bridge request with a
  **unique-per-attempt** `transaction`; `wait` matching response, 5 s timeout; classify
  ok/error/timeout; **idempotent** (safe to re-issue). Immediate path: 3 attempts, backoff 0.5 s / 1.5 s.
- **Arm hold** (Config Day/Night or Held-dim for a room; never double/triple) — set intent; remove the
  room's bulbs from its consolidated source group (best-effort 3-attempt). The tap already set the look.
- **Release hold** — triggers: **up-single ("tap on", immediate)** and **off→on (RESET_LED)**. Clear
  intent; re-add bulbs; **one-shot push current AL to the room's per-room group** for instant snap;
  on the off-path, restage AL color while off so next turn-on is adaptive.
- **Reconciler** — desired = overhead/accent bulbs whose room ∉ holds; actual from Z2M groups;
  converge with **one attempt per bulb per run** (run cadence is the cross-run retry). Runs on HA
  start, every 2nd tick (or own interval), and once after a partial-failure arm/release.
- **Failure notification** — one shared "failing bulbs" set. Warn once **on transition** into failing
  via `system_log.write` `level: warning`, `logger: light_man.hold`; one **~1 h escalation** if still
  failing; **silent retry** between; info line on recovery. No per-run spam (benign off-bulbs converge
  on power-up).
- **Entities/services** — `sensor` per-room hold status + a reconcile/health sensor; services
  `arm_hold` / `release_hold`; diagnostics dump.

**Existing tick blueprint: unchanged.** It floods whatever is in the group; membership does the exclusion.

## Phase 2+ — Migration roadmap (future, not committed by this approval)

- Move tick fan-out + write-on-change dedup (replace `input_text.al_last_published`) + drift-check into
  the coordinator.
- Move per-source pushes + per-room Inovelli unicast in; retire tick/push blueprints.
- Optionally migrate tap handling + occupancy. Decommission helpers/automations incrementally.
- Preserve KB-encoded fixes (latch, off-prestage, hue native control, SBM binding) as behavior +
  regression tests.

## Verification

- **Unit (pytest ≥95%, 100% config_flow):** retry state machine (ok/error/timeout/idempotent re-issue),
  reconciler diff/convergence, notification transition/suppression/escalation, hold arm/release.
- **Live:** robocopy deploy → `ha_restart` → `ha-integration-validator` "Validate light_man on live
  HA". Functional: set Day scene in a room → bulbs leave `zgb_overhead_all` (verify via Z2M groups) and
  the scene survives ≥2 ticks; tap-on → instant snap to AL + re-added; off→on → adapts; kill a bulb
  mid-hold → exactly one warning + reconciler converges on power-up.
- **Quality:** drive `light_man_audit.md` toward Silver.

## Open implementation decisions (resolve during build)

1. Tap seam: integration subscribes to Inovelli action topics vs. blueprint calls service.
2. Config surface for area/room ↔ bulb ↔ group/topic mapping (config entry + options vs imported YAML).
3. Confirm `integration_type` `hub` vs `service` via `ha-dev`.
