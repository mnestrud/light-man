#!/usr/bin/env bash
# Local compliance gate for Light Man — ruff format + ruff lint + mypy (strict) +
# pytest (+coverage >=95%). This is the per-change gate; GitHub Actions runs the
# same checks as a backstop. Run from the repo root:  ./scripts/check.sh
#
# Requires the venv (see CLAUDE.md "Running Tests Locally").
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="$repo_root/.venv/bin"
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

cd "$repo_root" || exit 1
run "ruff format (check)" "$venv/ruff" format --check .
run "ruff lint"           "$venv/ruff" check .
run "mypy (strict)"       "$venv/mypy" custom_components/light_man
run "pytest (+coverage)"  "$venv/pytest" tests/ -q

if [ "$fail" -ne 0 ]; then
  echo "Some checks failed."
  exit 1
fi
echo "All checks passed."
