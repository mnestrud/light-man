#!/usr/bin/env pwsh
# Local compliance gate for Light Man — ruff format + ruff lint + mypy (strict) +
# pytest (+coverage >=95%). This is the per-change gate; GitHub Actions is only a
# redundant backstop. Run from the repo root:  ./scripts/check.ps1
#
# Requires the venv (see CLAUDE.md "Running Tests Locally"). Native tools set
# $LASTEXITCODE rather than throwing, so each step is checked explicitly.

$ErrorActionPreference = "Stop"
$venv = Join-Path $PSScriptRoot "..\.venv\Scripts"

function Invoke-Step {
    param([string]$Name, [string]$Exe, [string[]]$Args)
    Write-Host "== $Name ==" -ForegroundColor Cyan
    & (Join-Path $venv $Exe) @Args
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

Invoke-Step "ruff format (check)" "ruff" @("format", "--check", ".")
Invoke-Step "ruff lint"           "ruff" @("check", ".")
Invoke-Step "mypy (strict)"       "mypy" @("custom_components/light_man")
Invoke-Step "pytest (+coverage)"  "pytest" @("tests/", "-q")

Write-Host "All checks passed." -ForegroundColor Green
