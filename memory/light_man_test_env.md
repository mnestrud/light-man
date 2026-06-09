# Light Man — Windows test/dev environment

Snapshot: 2026-06-09. The local toolchain as actually built (see `CLAUDE.md` → "Running Tests
Locally" for the canonical steps; this records the specifics + gotchas hit).

## Interpreter & venv

- **Python `3.13.13`** via `python3.13` (system `python` is 3.11 — do **not** use it; HA 2026.x needs
  3.13). Same interpreter particle-man's venv uses.
- venv at `.venv` (gitignored). Created with `python3.13 -m venv .venv`.
- **`sitecustomize.py`** copied from
  `C:\Users\micha\code\particle-man\.venv\Lib\site-packages\sitecustomize.py` — stubs Unix-only
  modules (fcntl/grp/pwd/resource/termios/tty) + Windows asyncio/socket patches. Without it,
  `ModuleNotFoundError: fcntl` at test collection.

## Installed (requirements_test.txt, pinned)

`pytest-homeassistant-custom-component==0.13.316`, `mypy==1.20.2`, `ruff==0.15.16`,
`pre-commit==4.6.0`. Pulled in transitively (no pin needed): **homeassistant 2026.2.3**, `paho-mqtt`
2.1.0, `astral` 2.2. `lru-dict` installed from a wheel (no C++ build needed this round; keep the
"Desktop development with C++" workload around in case a future dep needs compiling).

## Local compliance gate (no GitHub Actions in the loop)

- **pre-commit** (`.pre-commit-config.yaml`): ruff lint + format on every commit via the **pinned
  hosted `ruff-pre-commit`** (v0.15.16). Install once:
  `.venv/Scripts/pre-commit install --install-hooks`.
- **`scripts/check.sh`** (Git Bash): the full gate — ruff format-check + ruff lint + mypy-strict +
  pytest(+cov ≥95%). Run before pushing. (pytest is red until Phase-1 tests exist.)
- `pyproject.toml`: mypy `strict`; ruff strict select + `mccabe max-complexity=10`;
  `--cov-fail-under=95`. `scripts/*` ignore `T20` (prints allowed in dev utilities).

## Windows gotchas (cost real time — don't relearn)

- **Git Bash (MSYS) is the standard shell** (`C:\Program Files\Git\bin\bash.exe`). It auto-resolves
  `.exe`, so `.venv/Scripts/ruff` works in bash but **not** in pre-commit.
- **pre-commit can't spawn the relative venv path** (`.venv\Scripts\*.exe`): forward slashes fail
  `CreateProcess`, backslashes get stripped. → ruff runs via the hosted hook; mypy/pytest run via
  `check.sh` from the venv (they need the HA-aware venv anyway).
- **Windows Python can't read MSYS `/c/...` paths** — pass `C:/...` to `python3`/the venv python
  (e.g. reading `C:/Users/micha/.claude.json`). `cat`/coreutils handle `/c/...` fine.
- `.gitattributes` (`* text=auto eol=lf`) pins LF and silences the `core.autocrlf=true` CRLF warning.

## MQTT / HA access (for live testing)

- HA MCP wired via project `.mcp.json` (gitignored; `uvx ha-mcp`, token-based). Verified live.
- `scripts/mqtt_dump.py` — validated Z2M command helper; broker reached at `botworth:1883`
  (HA stores it as `core-mosquitto`; the helper rewrites to the host). See
  `docs/reference/z2m-mqtt-commands.md` and the `CLAUDE.md` "Device I/O" rule.
