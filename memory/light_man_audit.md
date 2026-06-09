# Light Man — Compliance & Quality Audit

Snapshot: 2026-06-09. This is the comprehensive, **sourced** compliance checklist for Light Man,
covering the Home Assistant Integration Quality Scale (all four tiers) and the HA Architecture Decision
Records (ADRs). It is a living document: update statuses as the build progresses, and keep every
**ignored** item documented with a reason so the decision is auditable for later work.

## Sources of truth

| Source | URL |
|---|---|
| Quality Scale — rules index | https://developers.home-assistant.io/docs/core/integration-quality-scale/rules |
| Quality Scale — per-rule detail | `https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/{rule-slug}` |
| Architecture Decision Records (ADRs) | https://github.com/home-assistant/architecture/tree/master/adr |
| ADR 0022 — quality scale framework | https://github.com/home-assistant/architecture/blob/master/adr/0022-integration-quality-scale.md |
| Lint ruleset (local) | `pyproject.toml` `[tool.ruff.lint]` + `memory/light_man_audit.md` (this file) |

Rule counts per the index at snapshot: **Bronze 19, Silver 10, Gold 24 (21 enumerated below — reconcile the 3-rule delta when Gold work begins), Platinum 3.**

## How compliance is enforced — local-first, NOT GitHub Actions

The per-change gate is **local** (GHA `validate.yml` is only a redundant backstop on push):

- **`pre-commit`** (`.pre-commit-config.yaml`): ruff **lint + format** on every commit via the pinned
  hosted `ruff-pre-commit` (matched to the venv's ruff). pre-commit's `language: system` can't spawn
  the relative Windows venv path, so mypy/pytest live in the full gate below.
- **`scripts/check.sh`** (Git Bash): the full gate — ruff format-check + ruff lint + mypy-strict +
  pytest (+coverage >=95%) — run from the venv before pushing.
- **`pyproject.toml`**: mypy `strict`; ruff curated strict select + `mccabe max-complexity = 10`;
  pytest `--cov-fail-under=95`.

## Status legend

- ✅ **done** — satisfied now (scaffold/config already in place)
- 🔜 **design-time** — committed in the design (PLAN/ARCHITECTURE); implement during the Phase-1 build
- 🛠 **polish-time** — mechanical; enforced continuously by ruff/mypy, no design decision needed
- ⬜ **todo** — not yet addressed (mostly docs / later tiers)
- 🚫 **ignored** — not applicable; reason recorded (see also the consolidated register at the end)

Target: **Silver before first internal release.** Gold/Platinum tracked but not gated yet.

---

## 🥉 Bronze (19)

| Rule | Status | Notes |
|---|---|---|
| `action-setup` | ✅ | `release_hold`/`clear_holds`/`force_push` registered in `async_setup_entry` (`services.py`, `async_register_services`); `services.yaml` present. |
| `appropriate-polling` | ✅ | Coordinator `update_interval = push_interval_s`, `always_update=False` (`coordinator.py`). |
| `brands` | 🚫 | Personal orchestrator, not submitted to `home-assistant/brands`. Revisit only if published to HACS/core. |
| `common-modules` | ✅ | `coordinator.py`, `const.py`, `entity.py` base — standard layout. |
| `config-flow-test-coverage` | ✅ | 100% on `config_flow` (`tests/test_config_flow.py`). |
| `config-flow` | ✅ | `config_flow: true`; single-instance confirm step (`single_config_entry` enforces the abort). |
| `dependency-transparency` | ✅ | `manifest.requirements: []`; only `dependencies: ["mqtt"]` (HA core). Nothing opaque. |
| `docs-actions` | ⬜ | Document the three services. (docs) |
| `docs-high-level-description` | ⬜ | README overview. (docs) |
| `docs-installation-instructions` | ⬜ | Install + prerequisites. (docs) |
| `docs-removal-instructions` | ⬜ | Uninstall guidance. (docs) |
| `entity-event-setup` | ✅ | MQTT subs are coordinator-owned (`_async_setup`), not entity-level; torn down in `async_unload_entry` via `shutdown_subscriptions()`. Entities are `CoordinatorEntity` (no direct event subs). |
| `entity-unique-id` | ✅ | `{entry_id}_{kind}` in `entity.py` (the `{room\|source}` slot is unused for the singletons). |
| `has-entity-name` | ✅ | `_attr_has_entity_name = True` on `LightManEntity`. |
| `runtime-data` | ✅ | Typed `LightManData` on `entry.runtime_data` (`__init__.py`); no `hass.data[DOMAIN]`. |
| `test-before-configure` | 🚫 | Phase-1 config is a singleton confirm with **no external connection to validate** (topology is Store-seeded; MQTT availability handled at runtime). Revisit in Phase 2 when the OptionsFlow adds validatable input. |
| `test-before-setup` | ✅ | `async_setup_entry` raises `ConfigEntryNotReady` on missing/invalid seed (`tests/test_init.py`). |
| `unique-config-entry` | ✅ | `single_config_entry: true`. |

## 🥈 Silver (10) — release target

| Rule | Status | Notes |
|---|---|---|
| `action-exceptions` | ✅ | `release_hold` raises `ServiceValidationError` (unknown room / not loaded); push wraps broker errors, never raising on a disconnected broker. |
| `config-entry-unloading` | ✅ | `async_unload_entry` unloads platforms, `shutdown_subscriptions()`, drops services on the last entry; the timer is cancelled by `coordinator.async_shutdown` (auto-registered on unload). |
| `docs-configuration-parameters` | ⬜ | Document seed-JSON fields. (docs) |
| `docs-installation-parameters` | ⬜ | Document setup. (docs) |
| `entity-unavailable` | ✅ | Entities are `CoordinatorEntity` (`available` = `last_update_success`). MQTT outage is surfaced via diagnostics, not entity-unavailable, so hold state stays visible. |
| `integration-owner` | ✅ | `codeowners: ["@mnestrud"]` in manifest. |
| `log-when-unavailable` | ✅ | `_set_mqtt_available` logs once on each MQTT connectivity transition (not per cycle). |
| `parallel-updates` | ✅ | `PARALLEL_UPDATES = 0` in `sensor.py` and `switch.py`. |
| `reauthentication-flow` | 🚫 | No integration-owned credentials; MQTT auth is owned by HA core. N/A. |
| `test-coverage` | ✅ | 100% coverage; local gate `--cov-fail-under=95`. |

## 🥇 Gold (24 per index; 21 enumerated) — future tier

| Rule | Status | Notes |
|---|---|---|
| `devices` | ✅ | One hub service device (`DeviceInfo`, `DeviceEntryType.SERVICE`) hosts both singletons (`entity.py`). |
| `diagnostics` | ✅ | `diagnostics.py` dumps push-health + hold state + config shape (no secrets) — ahead of tier. |
| `discovery` | 🚫 | Nothing to discover; topology is Store-seeded (Phase 1) / OptionsFlow (Phase 2). |
| `discovery-update-info` | 🚫 | No discovery. |
| `docs-data-update` | ⬜ | Document the push/adaptive model. (docs) |
| `docs-examples` | ⬜ | Automation examples. (docs) |
| `docs-known-limitations` | ⬜ | e.g. RF-during-holds, native-Hue coverage. (docs) |
| `docs-supported-devices` | ⬜ | Inovelli VZM31/VZM32 + Hue groups. (docs) |
| `docs-supported-functions` | ⬜ | Entities/services. (docs) |
| `docs-troubleshooting` | ⬜ | (docs) |
| `docs-use-cases` | ⬜ | (docs) |
| `dynamic-devices` | 🚫 | Fixed house topology from the seed; no runtime device addition. |
| `entity-category` | ✅ | active-holds = `DIAGNOSTIC`, push-enable = `CONFIG` (`sensor.py`/`switch.py`). |
| `entity-device-class` | 🚫 | No natural device class for a holds-count sensor or a stack toggle; N/A per-entity. |
| `entity-disabled-by-default` | 🔜 | Both singletons are intentionally enabled (control + at-a-glance holds). Revisit if noisier diagnostic entities are added. |
| `entity-translations` | ✅ | `translation_key` on both entities + `strings.json`/`translations/en.json` (ahead of tier). |
| `exception-translations` | ✅ | `not_loaded` / `unknown_room` `translation_key`s in `strings.json`; raised from `services.py` (ahead of tier). |
| `icon-translations` | ✅ | `icons.json` for entities + services; no `_attr_icon` on translated entities (ahead of tier). |
| `reconfiguration-flow` | ⬜ | The Phase-2 OptionsFlow (adaptive profiles) is the natural home. |
| `repair-issues` | ⬜ | Strong candidate: surface native-Hue orphan coverage / unmapped bulbs as a repair issue. |
| `stale-devices` | 🚫 | Only if/when `devices` is implemented; no device churn otherwise. |

## 🏆 Platinum (3) — future tier

| Rule | Status | Notes |
|---|---|---|
| `async-dependency` | ✅ | No external I/O dependency (MQTT via HA core). `astral` (Phase 2) is sync but CPU-only (<1 ms), not I/O. |
| `inject-websession` | 🚫 | No HTTP/websession used (MQTT only). N/A. |
| `strict-typing` | ✅ | mypy `strict` passes with `py.typed`, full annotations, and TypedDicts (config/hold/payload) — 0 errors. |

---

## ADR compliance (https://github.com/home-assistant/architecture/tree/master/adr)

| ADR | Status | Notes |
|---|---|---|
| 0002 / 0020 — minimum supported Python | ✅ | `target-version py313`, mypy `python_version 3.13`. |
| 0005 — code formatting | ✅ | `ruff format` enforced locally (pre-commit + check.ps1). |
| 0008 — code owners | ✅ | `manifest.codeowners: ["@mnestrud"]`. |
| 0009 — translations 2.0 | 🔜 | `strings.json` ↔ `translations/en.json` mirrored; entity/exception/icon translation keys. |
| 0010 — integration configuration | ✅/🔜 | UI config-flow; **no** YAML platform config. Phase-1 topology via Store seed; Phase-2 OptionsFlow. |
| 0022 — integration quality scale | ✅ | Adopted; tracked by this audit. Add `quality_scale` to manifest on reaching a tier. |

### ADRs ignored as not applicable (with reason)

| ADR | Reason ignored |
|---|---|
| 0001 — record architecture decisions | Process ADR for HA core itself; our equivalent is `docs/PLAN.md` + this audit. |
| 0003 — monitor condition / data selectors | Concerns HA frontend selectors; not relevant to this integration. |
| 0004 — webscraping | We do no web scraping. |
| 0006 — docker images | HA distribution policy, not integration code. |
| 0007 — integration config YAML structure | We expose **no** YAML platform config (config-flow only). |
| 0011 — discovery requires unique id | We implement no discovery. |
| 0012–0019 — installation methods / hardware screening / databases / GPIO | HA platform/policy ADRs; not applicable to a custom integration's code. |
| 0021 — YAML config deprecation policy | Never had YAML config to deprecate. |

---

## Consolidated "ignored / N-A" register (for later work)

Each is a deliberate exemption — revisit if the trigger condition changes.

| Item | Reason | Revisit when |
|---|---|---|
| `brands` (Bronze) | Personal integration, not in `home-assistant/brands`. | Publishing to HACS/core. |
| `test-before-configure` (Bronze) | Phase-1 config is a no-connection singleton confirm. | Phase 2 OptionsFlow adds validatable input. |
| `reauthentication-flow` (Silver) | No integration-owned auth; MQTT auth is core's. | Ever add an authenticated cloud/API source. |
| `discovery`, `discovery-update-info` (Gold) | Topology is seeded, not discovered. | Auto-discovery of Z2M groups/switches is added. |
| `dynamic-devices`, `stale-devices` (Gold) | Fixed house topology; no device churn. | If `devices` is implemented with dynamic membership. |
| `inject-websession` (Platinum) | MQTT only; no HTTP session. | Ever add an HTTP-based dependency. |
| ADRs 0001, 0003, 0004, 0006, 0007, 0011, 0012–0019, 0021 | Platform/policy or feature-specific ADRs not applicable to this integration's code. | If the relevant feature (YAML config, discovery, scraping, GPIO, etc.) is ever added. |

## Design-time rules to bake in from the first commit of each file

(Cheap now, costly to retrofit — see PLAN "Quality & compliance".)
`runtime-data` · `entity-unique-id` · `has-entity-name` · `entity-event-setup` · typed
`runtime_data` + TypedDicts (config/hold/payload) · `available` (entity-unavailable) ·
`action-exceptions` + exception `translation_key`s · `config-entry-unloading` cleanup ·
`parallel-updates` · tz-aware datetimes (DTZ) · lazy `%` logging (G/LOG) · diagnostics redaction.
