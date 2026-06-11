# Light Man — Claude Code Context

## MANDATORY SESSION STARTUP

Run ALL of these before responding to any user message.

1. `git -C "C:\Users\micha\code\light-man" status`
2. `git -C "C:\Users\micha\code\light-man" log --oneline -5`
3. Read `custom_components/light_man/manifest.json` → note version
4. Read memory file `light_man_audit.md` → note quality tier and any unverified items

**Output before anything else:**
```
STARTUP OK | branch: <name> | version: <x.y.z> | quality: silver-code-complete (docs+tag pending) | audit: <YYYY-MM-DD>
```
This checklist is not optional. "Resume directly" does not skip it.

---

## Repo Structure

| Path | Role |
|------|------|
| `custom_components/light_man/__init__.py` | Entry setup/unload, PLATFORMS, stale device cleanup |
| `custom_components/light_man/coordinator.py` | DataUpdateCoordinator, all API calls |
| `custom_components/light_man/config_flow.py` | ConfigFlow, OptionsFlow, reauth, reconfigure |
| `custom_components/light_man/const.py` | All constants and defaults |
| `custom_components/light_man/strings.json` | UI strings, exception translation keys |
| `custom_components/light_man/translations/en.json` | Mirrors strings.json (required by HA) |
| `custom_components/light_man/icons.json` | Icon translations — never use _attr_icon on translated entities |
| `tests/conftest.py` | Mock payloads, PHCC fixtures |
| `tests/test_*.py` | One file per source module |
| `.github/workflows/validate.yml` | CI: HACS, hassfest, ruff, mypy, pytest |
| `.github/workflows/claude-code-review.yml` | Auto PR review on open/sync |
| `.github/workflows/claude.yml` | @claude mentions in issues/PRs |
| `.github/workflows/docs.yml` | Deploy docs to GitHub Pages on main push |

Platforms: `sensor, switch` | Min HA: `2026.1` | Repo: `https://github.com/mnestrud/light-man`

---

## Running Tests Locally

**Working directory: `C:\Users\micha\code\light-man`**

### Windows Prerequisites (one-time, before creating the venv)

1. **C++ Build Tools** — required to build the `lru-dict` C extension that PHCC depends on.
   Via Visual Studio Installer → "Build Tools for Visual Studio 2022" → workload **"Desktop development with C++"** (includes MSVC compiler + Windows 11 SDK). If SDK is missing, `pip install` will fail even with Build Tools present.

2. **sitecustomize.py** — stubs Unix-only modules (fcntl, grp, pwd, resource, termios, tty) and patches asyncio/pytest-socket for Windows. After creating the venv, copy from an existing working venv:
   ```
   copy "C:\Users\micha\code\particle-man\.venv\Lib\site-packages\sitecustomize.py" ".venv\Lib\site-packages\sitecustomize.py"
   ```
   Without it: `ModuleNotFoundError: No module named 'fcntl'` at test collection.

3. **Pin PHCC and mypy versions** in `requirements_test.txt`:
   ```
   pytest-homeassistant-custom-component==0.13.316
   mypy==1.20.2
   ```

```bash
# First time — create venv
python -m venv .venv
.venv/Scripts/pip install -r requirements_test.txt
# Windows: copy sitecustomize.py as described above

# Type check (strict — flags come from pyproject.toml [tool.mypy])
.venv/Scripts/mypy custom_components/light_man

# All tests
.venv/Scripts/pytest tests/ -q

# With coverage
.venv/Scripts/pytest tests/ --cov=custom_components/light_man --cov-report=term-missing -q

# Stop on first failure
.venv/Scripts/pytest tests/ -x --tb=short -q
```

Target: ≥95% coverage overall; 100% on config_flow. Any PR to main must hit this.

---

## PHCC Gotchas (applies to every HA integration)

- `AiohttpClientMockResponse` has **no `.ok`** — use `resp.status < 400` in source; never `resp.ok`
- `MockConfigEntry.options` is **read-only** — pass all options at construction time
- `DataUpdateCoordinator` requires `config_entry=` kwarg (HA 2026.x)
- `Store` must be patched: `patch("custom_components.light_man.coordinator.Store", autospec=True)`
- `auto_enable_custom_integrations` must be `autouse=True` in conftest — required for config entry setup to find the integration
- Windows: add `event_loop_policy` fixture to set `WindowsSelectorEventLoopPolicy`
- `aioclient_mock` URL matching is case-sensitive — use `re.compile(..., re.IGNORECASE)` for mixed-case paths
- Add `-p no:socket` to `addopts` in pyproject.toml to catch missed mocks
- `hass.config.units` has no `name` or `is_metric` in HA 2026.x — use integration-defined default constants instead of detecting locale from UnitSystem
- **Eager argument evaluation:** `dict.get(key, self._get(...))` always evaluates `_get()` even when key exists — use explicit `if key in dict` guard
- **`strings.json` / `en.json` duplicate step key:** never have two `"step"` keys under `"config"` — JSON parsers silently drop the first. All steps (`user`, `reauth_confirm`, `reconfigure`) must be siblings under a single `"step"` key.
- **`EntityCategory`** imports from `homeassistant.const`, not `homeassistant.helpers.entity` (deprecated path causes mypy strict error)

---

## ADR Compliance Patterns

Patterns validated against HA ADRs and quality scale rules. Apply from day one — cheaper than fixing later.

### manifest.json
- Always include `integration_type` — `"service"` for cloud APIs, `"hub"` for local gateways, `"device"` for single-device integrations. hassfest is tightening this requirement.
- Include `"quality_scale"` once you reach a tier (`"bronze"` / `"silver"` / `"gold"` / `"platinum"`).

### pyproject.toml — mypy config (must be in repo root, not inside the component)
```toml
[tool.mypy]
python_version = "3.13"
strict = true
ignore_missing_imports = true
```
Mypy searches upward from the working directory for config. A `mypy.ini` inside `custom_components/<domain>/` is invisible to CI and to anyone running from the repo root.

### Coordinator
- **`_async_setup()`** — put one-time async initialization (Store loading, auth checks, connection tests) in `_async_setup()` instead of calling a custom method manually before `async_config_entry_first_refresh()`. The framework calls it automatically; errors become `ConfigEntryNotReady` (automatic retry).
- **`always_update=False`** — add to `super().__init__()` if the coordinator can return unchanged cached data (quiet hours, backoff periods). Prevents needless entity state writes on every poll.
- **Parallel init** — for multi-device/multi-location setups, create all coordinators first, then refresh in parallel:
  ```python
  await asyncio.gather(*(c.async_config_entry_first_refresh() for c in coordinators.values()))
  ```

### hass.data cleanup
```python
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and not hass.config_entries.async_entries(DOMAIN):
        hass.data.pop(DOMAIN, None)
    return unload_ok
```

---

## Branch and PR Workflow

- **`dev`** — all development. Never commit directly to main.
- **`main`** — merged from dev via PR only; always tagged with a release.
- Feature branches: from dev, PR back to dev.

**PR checklist before merging dev → main:**
- [ ] All CI checks pass (validate workflow)
- [ ] ≥95% test coverage, 0 mypy strict errors
- [ ] `manifest.json` version bumped (semver)
- [ ] `manifest.json` has `integration_type` set correctly
- [ ] Docs updated if behavior or config changed
- [ ] Audit memory file updated if any rule status changed

---

## CI/CD Workflows

| Workflow | Trigger | What it checks |
|----------|---------|----------------|
| `validate.yml` | Every push + PR | HACS → hassfest → ruff → mypy → pytest |
| `claude-code-review.yml` | PR open/sync | Automated Claude review comment |
| `claude.yml` | @claude in issues/PRs | Responds to @claude mentions |
| `docs.yml` | Push to main (docs/** or mkdocs.yml) | Deploys GitHub Pages |

**Common CI failures:**
- `hassfest`: manifest.json version format wrong, or missing required field
- `HACS`: missing `hacs.json`, brand assets in wrong path, or missing README
- `mypy`: untyped dict access, missing `from __future__ import annotations`, wrong return type
- `pytest passes locally, fails CI`: PHCC version drift — pin `requirements_test.txt` to a specific version

---

## Quality Scale

**Current tier: Silver code-complete** — every Bronze + Silver *code* rule is ✅ or justified N/A in
`memory/light_man_audit.md`. The remaining gap is **documentation** (4 Bronze + 2 Silver docs items:
README / install / removal / config-params / etc.) plus tagging `manifest.json` with
`"quality_scale": "silver"` once those docs land. Not yet formally claimed in the manifest.

Full audit checklist (56 rules, 4 tiers): read `memory/light_man_audit.md`
Quality scale rules: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules

Target progression: Silver before first release → Gold code-complete before v1.0 → Platinum ongoing.

---

## Common Task Patterns

### Add a new sensor
1. Add constant to `const.py`
2. Add sensor class — inherit from existing base, set `_attr_translation_key`, `_attr_entity_category`, `_attr_device_class`, `_attr_entity_registry_enabled_default`
3. Add translation key to `strings.json` and `translations/en.json` under `entity.sensor.<key>`
4. Add to `icons.json` under `entity.sensor.<key>` if custom icon needed — do **not** use `_attr_icon` on translated entities
5. Update coordinator to populate the data field
6. Write test covering entity properties and state
7. Run: `pytest tests/test_sensor.py -q --tb=short`

### Modify config/options flow
1. Edit `config_flow.py` — all four flows live here (user, reauth, reconfigure, options)
2. Update `strings.json` step schema and error keys; mirror to `translations/en.json`
3. If adding config key: add to `const.py` with default, update `_opt()` helper in `__init__.py`
4. Run: `pytest tests/test_config_flow.py tests/test_options_flow.py -q`

### Add an API endpoint
1. Add URL/constants to `const.py`
2. Add fetch method to `coordinator.py` — use `resp.status < 400` not `resp.ok`
3. Add mock response in `conftest.py` `register_api_mocks()`
4. Write coordinator tests for success, HTTP error (4xx/5xx), and quota-block paths

### Fix a mypy error
- Run: `.venv/Scripts/mypy custom_components/light_man`
- Do not add `# type: ignore` without an explanatory comment
- Common causes: dict access without guard, missing `| None`, no `from __future__ import annotations`

---

## Agent Usage

| When | Use |
|------|-----|
| HA entity API signatures, coordinator/flow patterns, HA breaking changes | `ha-dev` agent |
| External API schemas, field names, quota details | integration-specific agent or direct research |
| After robocopy deploy + restart confirmed | `ha-integration-validator` agent |
| General Python/testing questions | Answer directly — no agent |

Invoke agents with the Agent tool (`subagent_type: ha-dev`). Don't answer HA API questions from training data — HA APIs change frequently.

---

## Development and Deploy Workflow

**Source of truth: git repo. Test target: live HA via Samba. These are two separate steps.**

### Step 1 — Edit and test locally
1. Edit files in `C:\Users\micha\code\light-man/custom_components/light_man/`
2. Run `pytest tests/ -q --tb=short` to catch regressions

### Step 2 — Deploy to live HA for integration testing
```bash
# Mirror integration source to live HA
robocopy "C:\Users\micha\code\light-man\custom_components\light_man" "\\botworth\config\custom_components\light_man" /MIR /NFL /NDL
```
- **Config ownership:** today the git-bundled `light_man_config.json` is the source of truth, so `/MIR`
  deploys it normally. **Once the web panel owns device config** (writes the Store + a device config file),
  add `/XF light_man_config.json` to this mirror so a code deploy never clobbers panel edits — and the
  loader must then **migrate the Store in place** on a `seed_version` bump, not overwrite it. See
  `docs/reference/data-model.md` → "Config persistence & files".
- **Python changes** (any `.py` file): full HA restart required — use `ha_restart` MCP call; do NOT poll after, tell user to confirm when ready
- **Non-Python changes** (strings.json, translations, icons): reload only — `ha_reload_config component=core`

### Step 2b — Validate on live HA (after user confirms restart complete)
Invoke `ha-integration-validator` agent: "Validate light_man on live HA"

- PASS → proceed to commit
- WARN → confirm with user whether unavailable entities are expected, then commit
- FAIL → investigate errors before committing; do not push to git until resolved

### Step 3 — Commit and push
```bash
git add <changed files>
git commit -m "..."
git push origin dev
```
Never commit to main directly. Open a PR (dev → main) when ready for release.

### What NOT to do
- Do not edit Samba directly — git repo is source of truth; Samba is deploy target only
- No `ha_write_file`, no patch subagents, no MCP file writes

---

## Device I/O — Zigbee via MQTT (not HA state)

For any **Zigbee device** (Z2M-backed: Hue bulbs, Inovelli switches), read and write state/attributes
**over MQTT**, never via HA entity state or the MCP `ha_*` tools — HA's attributes are an optimistic
echo layer on top of Z2M (e.g. `hue_native_control` color is an echo). MQTT/Z2M is the device's ground
truth. (HA-internal entities — Adaptive Lighting *dummy* switches, `input_boolean`s, template/helper
entities — aren't Zigbee; use HA/MCP for those.)

Use **`scripts/mqtt_dump.py`** (validated against the live converter data; reads read-only, writes need
`--commit`). **Never hand-type raw `/set` payloads** — the helper only allows commands the device's own
exposes/options declare. Canonical command language: `docs/reference/z2m-mqtt-commands.md`.

Two layers (see the reference): **state** (`exposes` → `zigbee2mqtt/<name>/set`) and **options**
(`ea.SET` config like `hue_native_control` → `bridge/request/{device,group}/options`, read from
`configuration.yaml`). For a group, `/set` keys are the **union of member exposes**.

**Group commands are Zigbee multicasts (network broadcasts), not unicasts.** Every bulb on the mesh
hears them; group membership is a **receiver-side filter** the bulb applies from its own NVRAM group
table — which can **drift** from Z2M's `bridge/groups` view. A bulb can therefore apply a group's
command (e.g. flip to the hallway's rgb) for a group Z2M doesn't list it in. Reconcile with
`bridge/request/group/members/remove_all` then re-add to the correct groups (used 2026-06-09 to fix
the living-room color-mode split; Phase 2 normalizes house-wide). **Read true device color via `/get`**,
not the retained state — with `optimistic: true` bulbs the retained payload echoes the *commanded*
value, not what the silicon settled on (confirm a fresh message arrived). `mqtt_dump.py` has no
group-membership helper yet — those go via raw `bridge/request/group/members/*`. Full trace:
`docs/reference/bulb-split-investigation.md`.

```
python scripts/mqtt_dump.py caps '<name>'                 # what's settable (per model)
python scripts/mqtt_dump.py sub  'zigbee2mqtt/<name>'     # current state (retained = truth)
python scripts/mqtt_dump.py set  '<name>' '<json>'        # dry-run; add --commit to apply + read back
python scripts/mqtt_dump.py options '<group>'             # read hue_native_control etc.
```

Publishing changes a real device — confirm before `--commit` unless told to proceed.
