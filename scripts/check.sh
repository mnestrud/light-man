#!/usr/bin/env bash
# Local compliance gate for Light Man — ruff format + ruff lint + mypy (strict) +
# pytest (+coverage >=95%). This is the per-change gate; GitHub Actions is only a
# redundant backstop. Run from the repo root (Git Bash):  ./scripts/check.sh
#
# Requires the venv (see CLAUDE.md "Running Tests Locally"). MSYS/Git Bash resolves
# the .exe suffix automatically, so the Windows venv tools are called by bare name.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="$repo_root/.venv/Scripts"
fail=0

run() {
  local name="$1"; shift
  echo "== $name =="
  "$@"
  local rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "FAILED: $name (exit $rc)"
    fail=1
  fi
}

run "ruff format (check)" "$venv/ruff" format --check .
run "ruff lint"           "$venv/ruff" check .
run "mypy (strict)"       "$venv/mypy" custom_components/light_man
run "pytest (+coverage)"  "$venv/pytest" tests/ -q

if [ "$fail" -ne 0 ]; then
  echo "Some checks failed."
  exit 1
fi
echo "All checks passed."
