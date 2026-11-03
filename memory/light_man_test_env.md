# Light Man — test/dev environment (Linux, patgod)

Snapshot: 2026-11-03. See `CLAUDE.md` → "Running Tests Locally" for the canonical steps; this records
the specifics and gotchas.

## Interpreter & venv

- **Python 3.14.7** managed by `uv` (`uv venv -p 3.14 .venv`). PHCC 0.13.375 requires ≥ 3.14.
- `.venv` (gitignored). The previous 3.13 venv is kept as `.venv-py313-rollback/` (gitignored) until
  0.7.9 has been live for a while; delete it after that.
- Install: `uv pip install -p .venv/bin/python -r requirements_test.txt`.

## Installed (requirements_test.txt, pinned)

`pytest-homeassistant-custom-component==0.13.375` (pulls **homeassistant 2026.10.4**, pytest 9.0.3,
pytest-asyncio 1.4.0, pytest-socket 0.8.1), `mypy==1.20.2`, `ruff==0.15.16`, `pre-commit==4.6.0`.
`paho-mqtt` and `astral` arrive transitively.

## Gate

- **pre-commit**: ruff lint + format via the pinned hosted `ruff-pre-commit` (v0.15.16).
  `.venv/bin/pre-commit install`.
- **`scripts/check.sh`**: ruff format-check + ruff lint + mypy strict + pytest (cov ≥ 95 %, currently
  100 %). CI (`validate.yml`) runs the same tools from `requirements_test.txt` on Python 3.14.

## Gotchas hit on the 2026.10 upgrade (2026-11-03)

- HA 2026.9 moved schemas to **probatio**: `websocket_command` and `async_register(schema=...)` type-check
  only against `probatio` markers. Use `import probatio as vol`. hacs.json minimum raised to 2026.10.0.
- `StaticPathConfig` is re-exported from `homeassistant.components.http` without `__all__`; strict mypy
  needs a commented `type: ignore[attr-defined]` (core imports the same path).
- PHCC 0.13.375 fails every MQTT test with "Lingering timer ... `_async_start_misc_periodic`": HA's MQTT
  client cancels its 1 s misc timer on socket close, which the mocked paho client never does. Fixed by
  the `mqtt_mock` override in `tests/conftest.py` that cancels the timer at teardown.
- `get_astral_location` is deprecated (removed 2027.7); `solar.py` imports `get_astral_observer` directly.
- The Windows-only `event_loop_policy` fixture was removed (pytest-asyncio 1.4 deprecates overriding it).

## Live access

See CLAUDE.md "Live System Map & Logs": HA at `https://meddle.home.botworth.net` (token `~/.ha-token`),
logs via `scripts/ha_logs.sh`, MQTT via `scripts/mqtt_dump.py` (run with `.venv/bin/python`).
