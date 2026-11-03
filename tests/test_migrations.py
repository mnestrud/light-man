"""Tests for in-place Store migration across seed_version bumps."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from custom_components.light_man.adaptive import compute_target
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import BUNDLED_SEED_VERSION
from custom_components.light_man.migrations import (
    MigrationError,
    migrate,
    stored_version,
)

FIXTURES = Path(__file__).parent / "fixtures"
BUNDLE = Path(__file__).parents[1] / "custom_components/light_man/light_man_config.json"


def _seed_v8() -> dict:
    return json.loads((FIXTURES / "seed_v8.json").read_text())


def test_v8_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v8())
    assert steps == [8]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["seed_version"] == BUNDLED_SEED_VERSION


def test_v8_to_v9_makes_day_window_defaults_explicit() -> None:
    migrated, _ = migrate(_seed_v8())
    for curve in migrated["curves"].values():
        assert "sat" in curve
        assert curve["day_window"]["edge_transition_s"] == 1800.0
        assert curve["day_window"]["wind_down_s"] == 5400.0


def test_migration_is_engine_identical() -> None:
    old = validate_config(_seed_v8())
    new = validate_config(migrate(_seed_v8())[0])
    for key, source in old.config["sources"].items():
        new_profile = new.config["sources"][key]["profile"]
        for minutes in range(0, 24 * 60, 7):
            for elevation in (-12.0, -3.0, 5.0, 25.0, 60.0):
                for sleep_s in (0.0, 0.4, 1.0):
                    a = compute_target(
                        elevation,
                        60.0,
                        source["profile"],
                        sleep_s=sleep_s,
                        now_minutes=float(minutes),
                    )
                    b = compute_target(
                        elevation,
                        60.0,
                        new_profile,
                        sleep_s=sleep_s,
                        now_minutes=float(minutes),
                    )
                    assert a == b


def test_migrate_keeps_edits_and_does_not_mutate_input() -> None:
    raw = _seed_v8()
    raw["curves"]["daylight_standard"]["max_br"] = 77
    raw["curves"]["daylight_standard"]["day_window"]["wind_down_s"] = 60
    before = copy.deepcopy(raw)
    migrated, _ = migrate(raw)
    assert raw == before
    assert migrated["curves"]["daylight_standard"]["max_br"] == 77
    assert migrated["curves"]["daylight_standard"]["day_window"]["wind_down_s"] == 60


def test_migrate_tolerates_odd_curves() -> None:
    raw = {"seed_version": 8, "curves": {"bad": "nope", "no_window": {}}}
    migrated, _ = migrate(raw)
    assert migrated["curves"]["bad"] == "nope"
    assert migrated["curves"]["no_window"] == {"sat": 0.5}


def test_current_version_is_a_no_op() -> None:
    raw = migrate(_seed_v8())[0]
    again, steps = migrate(raw)
    assert steps == []
    assert again == raw


@pytest.mark.parametrize("raw", [{}, {"seed_version": 7}, {"seed_version": "8"}])
def test_too_old_raises(raw: dict) -> None:
    with pytest.raises(MigrationError, match="predates"):
        migrate(raw)


def test_gap_raises() -> None:
    with pytest.raises(MigrationError, match="no migration"):
        migrate({"seed_version": 8}, target=BUNDLED_SEED_VERSION + 1)


def test_stored_version() -> None:
    assert stored_version({"seed_version": 9}) == 9
    assert stored_version({"seed_version": "9"}) == 0
    assert stored_version({}) == 0
