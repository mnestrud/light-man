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
    BATH_FRONT,
    BATH_TOILET,
    HALL_EAST,
    HALL_WEST,
    STAIRWELL_TOP,
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
    assert steps == [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["seed_version"] == BUNDLED_SEED_VERSION


def test_v8_to_v9_makes_day_window_defaults_explicit() -> None:
    migrated, _ = migrate(_seed_v8(), target=9)
    for curve in migrated["curves"].values():
        assert "sat" in curve
        assert curve["day_window"]["edge_transition_s"] == 1800.0
        assert curve["day_window"]["wind_down_s"] == 5400.0


def test_v9_fixture_migrates_to_the_shipped_bundle() -> None:
    raw = json.loads((FIXTURES / "seed_v9.json").read_text())
    migrated, steps = migrate(raw)
    assert steps == [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["push_interval_s"] == 240


def test_v9_to_v10_keeps_an_edited_interval() -> None:
    raw = json.loads((FIXTURES / "seed_v9.json").read_text())
    raw["push_interval_s"] = 45
    assert migrate(raw)[0]["push_interval_s"] == 45


def test_v10_fixture_migrates_to_the_shipped_bundle() -> None:
    raw = json.loads((FIXTURES / "seed_v10.json").read_text())
    migrated, steps = migrate(raw)
    assert steps == [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    hall = migrated["occupancy_zones"]["hallway"]
    assert hall["override_switches"] == [HALL_EAST, HALL_WEST]
    assert hall["sync_switches"] == [HALL_EAST]
    assert migrated["occupancy_zones"]["stairwell"]["sync_switches"] == [STAIRWELL_TOP]
    assert validate_config(migrated).issues == []


def test_v10_to_v11_leaves_an_edited_hallway_and_stairwell_alone() -> None:
    raw = json.loads((FIXTURES / "seed_v10.json").read_text())
    raw["rooms"]["hallway"]["switches"] = [
        {"topic": HALL_EAST, "governs": "hallway.east_up"}
    ]
    raw["rooms"]["stairwell"]["switches"] = []
    migrated = migrate(raw)[0]
    assert "override_switches" not in migrated["occupancy_zones"]["hallway"]
    assert "sync_switches" not in migrated["occupancy_zones"]["stairwell"]
    assert (
        migrated["rooms"]["hallway"]["switches"] == raw["rooms"]["hallway"]["switches"]
    )


def test_migration_is_engine_identical() -> None:
    old = validate_config(_seed_v8())
    new = validate_config(migrate(_seed_v8(), target=9)[0])
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


def _seed_v12() -> dict:
    return json.loads((FIXTURES / "seed_v12.json").read_text())


def _bath_switches(raw: dict) -> list[dict]:
    return raw["rooms"]["primary_bath"]["switches"]


def test_v12_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v12())
    assert steps == [12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    bath = ["primary_bath.overhead", "primary_bath.under_vanity"]
    switches = {sw["topic"]: sw for sw in _bath_switches(migrated)}
    assert switches[BATH_FRONT]["governs"] == bath
    assert switches[BATH_TOILET]["governs"] == bath
    for switch in migrated["rooms"]["hallway"]["switches"]:
        assert switch["bar_follows"] == "hallway.east_down"
    result = validate_config(migrated)
    assert result.issues == []
    assert result.bar_lights[HALL_EAST] == ("hallway_down", "hallway.east_down")


def test_v12_to_v13_leaves_edited_switches_alone() -> None:
    raw = _seed_v12()
    _bath_switches(raw)[0]["governs"] = "primary_bath.shower"
    for switch in raw["rooms"]["hallway"]["switches"]:
        switch["bar_follows"] = "hallway.west_up"
    migrated = migrate(raw)[0]
    assert [sw["topic"] for sw in _bath_switches(migrated)] == [
        sw["topic"] for sw in _bath_switches(raw)
    ]
    for switch in migrated["rooms"]["hallway"]["switches"]:
        assert switch["bar_follows"] == "hallway.west_up"


def test_v12_to_v13_keeps_a_toilet_switch_already_added() -> None:
    raw = _seed_v12()
    toilet = {"topic": BATH_TOILET, "governs": "primary_bath.overhead"}
    _bath_switches(raw).append(toilet)
    migrated = migrate(raw)[0]
    topics = [sw["topic"] for sw in _bath_switches(migrated)]
    assert topics.count(BATH_TOILET) == 1
    assert _bath_switches(migrated)[-1] == toilet


def test_v12_to_v13_skips_rooms_that_are_gone() -> None:
    raw = _seed_v12()
    del raw["rooms"]["primary_bath"]
    del raw["rooms"]["hallway"]
    migrated = migrate(raw)[0]
    assert "primary_bath" not in migrated["rooms"]


def _seed_v13() -> dict:
    return json.loads((FIXTURES / "seed_v13.json").read_text())


def test_v13_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v13())
    assert steps == [13, 14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["push_interval_s"] == 240


def test_v13_to_v14_keeps_an_edited_interval() -> None:
    raw = _seed_v13()
    raw["push_interval_s"] = 90
    assert migrate(raw)[0]["push_interval_s"] == 90


def _seed_v14() -> dict:
    return json.loads((FIXTURES / "seed_v14.json").read_text())


def test_v14_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v14())
    assert steps == [14, 15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    sensors = migrated["rooms"]["stairwell"]["sensors"]
    assert sensors["stairwell_top_landing"]["occupancy_key"] == "mmwave_area2_occupancy"
    zone = migrated["occupancy_zones"]["stairwell"]
    assert zone["on_transition_s"] == 0.5
    assert (
        zone["sensors"]["stairwell.stairwell_top_landing"]
        == zone["sensors"]["stairwell.stairwell_top"]
    )
    result = validate_config(migrated)
    assert result.issues == []


def test_v14_to_v15_keeps_an_edited_fade_and_landing() -> None:
    raw = _seed_v14()
    zone = raw["occupancy_zones"]["stairwell"]
    zone["on_transition_s"] = 1.0
    landing = {"topic": "zigbee2mqtt/Other", "occupancy_key": "x"}
    raw["rooms"]["stairwell"]["sensors"]["stairwell_top_landing"] = landing
    migrated = migrate(raw)[0]
    assert migrated["occupancy_zones"]["stairwell"]["on_transition_s"] == 1.0
    assert migrated["rooms"]["stairwell"]["sensors"]["stairwell_top_landing"] == landing


def test_v14_to_v15_skips_an_edited_stairwell() -> None:
    raw = _seed_v14()
    raw["rooms"]["stairwell"]["sensors"]["stairwell_top"]["topic"] = "zigbee2mqtt/X"
    migrated = migrate(raw)[0]
    assert "stairwell_top_landing" not in migrated["rooms"]["stairwell"]["sensors"]
    assert "on_transition_s" not in migrated["occupancy_zones"]["stairwell"]


def test_v14_to_v15_skips_when_the_stairwell_is_gone() -> None:
    raw = _seed_v14()
    del raw["occupancy_zones"]["stairwell"]
    assert "stairwell" not in migrate(raw)[0]["occupancy_zones"]
    raw = _seed_v14()
    raw["rooms"]["stairwell"]["sensors"] = []
    assert migrate(raw)[0]["rooms"]["stairwell"]["sensors"] == []
    raw = _seed_v14()
    del raw["occupancy_zones"]["stairwell"]["sensors"]["stairwell.stairwell_top"]
    migrated = migrate(raw)[0]
    assert "stairwell_top_landing" not in migrated["rooms"]["stairwell"]["sensors"]


def _seed_v15() -> dict:
    return json.loads((FIXTURES / "seed_v15.json").read_text())


def test_v15_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v15())
    assert steps == [15, 16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert validate_config(migrated).config["steps"] == {
        "min_s": 30.0,
        "max_brightness_pct": 2.0,
        "max_mired": 5.0,
        "max_xy": 0.005,
    }


def test_v15_to_v16_keeps_an_edited_step_block() -> None:
    raw = _seed_v15()
    raw["steps"] = {"min_s": 60}
    assert migrate(raw)[0]["steps"] == {"min_s": 60}


def _seed_v16() -> dict:
    return json.loads((FIXTURES / "seed_v16.json").read_text())


def test_v16_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v16())
    assert steps == [16, 17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    for curve in migrated["curves"].values():
        assert curve["day_window"]["start"] == "07:30"
        assert curve["day_window"]["edge_transition_s"] == 3600.0
    assert validate_config(migrated).issues == []


def test_v16_to_v17_keeps_an_edited_window() -> None:
    raw = _seed_v16()
    curves = list(raw["curves"].values())
    curves[0]["day_window"]["start"] = "06:45"
    curves[1]["day_window"]["edge_transition_s"] = 900.0
    del curves[2]["day_window"]
    migrated = list(migrate(raw)[0]["curves"].values())
    assert migrated[0]["day_window"]["start"] == "06:45"
    assert migrated[0]["day_window"]["edge_transition_s"] == 1800.0
    assert migrated[1]["day_window"]["start"] == "08:00"
    assert "day_window" not in migrated[2]
    assert migrated[3]["day_window"]["start"] == "07:30"


def test_v16_to_v17_skips_bad_curves() -> None:
    raw = _seed_v16()
    raw["curves"] = "nope"
    assert migrate(raw)[0]["curves"] == "nope"
    raw = _seed_v16()
    raw["curves"]["odd"] = "not-a-curve"
    assert migrate(raw)[0]["curves"]["odd"] == "not-a-curve"


def _seed_v17() -> dict:
    return json.loads((FIXTURES / "seed_v17.json").read_text())


def test_v17_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v17())
    assert steps == [17, 18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["occupancy_zones"]["hallway"]["on_transition_s"] == 1.0
    assert validate_config(migrated).issues == []


def test_v17_to_v18_keeps_an_edited_fade() -> None:
    raw = _seed_v17()
    raw["occupancy_zones"]["hallway"]["on_transition_s"] = 2.0
    assert migrate(raw)[0]["occupancy_zones"]["hallway"]["on_transition_s"] == 2.0


def test_v17_to_v18_skips_a_missing_hallway_zone() -> None:
    raw = _seed_v17()
    del raw["occupancy_zones"]["hallway"]
    assert "hallway" not in migrate(raw)[0]["occupancy_zones"]
    raw["occupancy_zones"] = {"hallway": "nope"}
    assert migrate(raw)[0]["occupancy_zones"]["hallway"] == "nope"


def test_v17_to_v18_moves_the_stair_binding_to_west_area2() -> None:
    migrated = migrate(_seed_v17())[0]
    stair = migrated["occupancy_zones"]["stairwell"]["sensors"]
    assert "hallway.hall_west" not in stair
    assert "hallway.hall_west_stairs" in stair
    assert "hallway.hall_west" in migrated["occupancy_zones"]["hallway"]["sensors"]
    assert migrated["rooms"]["hallway"]["sensors"]["hall_west_stairs"] == {
        "topic": "zigbee2mqtt/Hall Smart Switch West New mmwave",
        "occupancy_key": "mmwave_area2_occupancy",
    }


def test_v17_to_v18_leaves_edited_stair_bindings() -> None:
    raw = _seed_v17()
    del raw["occupancy_zones"]["stairwell"]["sensors"]["hallway.hall_west"]
    migrated = migrate(raw)[0]
    assert "hall_west_stairs" not in migrated["rooms"]["hallway"]["sensors"]
    raw = _seed_v17()
    del raw["rooms"]["hallway"]["sensors"]["hall_west"]
    migrated = migrate(raw)[0]
    assert "hallway.hall_west" in migrated["occupancy_zones"]["stairwell"]["sensors"]
    raw = _seed_v17()
    raw["occupancy_zones"]["stairwell"] = "nope"
    assert migrate(raw)[0]["occupancy_zones"]["stairwell"] == "nope"


def _seed_v18() -> dict:
    return json.loads((FIXTURES / "seed_v18.json").read_text())


def test_v18_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v18())
    assert steps == [18, 19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    assert migrated["occupancy_zones"]["hallway"]["on_transition_s"] == 1.0


def test_v18_to_v19_keeps_an_edited_fade() -> None:
    raw = _seed_v18()
    raw["occupancy_zones"]["hallway"]["on_transition_s"] = 2.0
    assert migrate(raw)[0]["occupancy_zones"]["hallway"]["on_transition_s"] == 2.0
    del raw["occupancy_zones"]["hallway"]
    assert "hallway" not in migrate(raw)[0]["occupancy_zones"]


def _seed_v19() -> dict:
    return json.loads((FIXTURES / "seed_v19.json").read_text())


def test_v19_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v19())
    assert steps == [19, 20]
    assert migrated == json.loads(BUNDLE.read_text())
    stage = migrated["occupancy_zones"]["hallway"]["sensors"]["hallway.hall_east"][
        "sweep"
    ][1]
    assert stage["group"] == "zigbee2mqtt/zgb_hall_center_pair/set"


def test_v19_to_v20_leaves_edited_and_odd_stages_alone() -> None:
    raw = _seed_v19()
    sweep = raw["occupancy_zones"]["hallway"]["sensors"]["hallway.hall_west"]["sweep"]
    sweep[0]["group"] = "zigbee2mqtt/mine/set"
    sweep[1]["lights"] = ["hallway.center_up"]
    sweep[2]["lights"] = ["hallway.east_up", "hallway.west_down"]
    sweep.append("nope")
    sweep.append({"lights": "nope"})
    migrated = migrate(raw)[0]
    out = migrated["occupancy_zones"]["hallway"]["sensors"]["hallway.hall_west"][
        "sweep"
    ]
    assert out[0]["group"] == "zigbee2mqtt/mine/set"
    assert "group" not in out[1]
    assert "group" not in out[2]
    raw = _seed_v19()
    raw["occupancy_zones"]["hallway"]["sensors"]["hallway.hall_east"] = "nope"
    raw["occupancy_zones"]["hallway"]["sensors"]["hallway.hall_west"]["sweep"] = 3
    migrate(raw)
    raw["occupancy_zones"]["hallway"]["sensors"] = "nope"
    migrate(raw)
    del raw["occupancy_zones"]["hallway"]
    assert "hallway" not in migrate(raw)[0]["occupancy_zones"]


def _seed_v20() -> dict:
    return json.loads((FIXTURES / "seed_v20.json").read_text())


def test_v20_fixture_migrates_to_the_shipped_bundle() -> None:
    migrated, steps = migrate(_seed_v20())
    assert steps == [20]
    assert migrated == json.loads(BUNDLE.read_text())
    zone = migrated["occupancy_zones"]["primary_bath"]
    assert zone["override_until_off"] is True
    assert zone["sensors"]["primary_bath.bath_front"]["sweep"][0] == {
        "lights": ["primary_bath.toilet"],
        "brightness_pct": 10,
    }


def test_v20_to_v21_keeps_existing_pieces_and_skips_odd_shapes() -> None:
    raw = _seed_v20()
    room = raw["rooms"]["primary_bath"]
    room["lights"].append({"id": "toilet", "set_topic": "mine", "source": "overhead"})
    room["sensors"] = "nope"
    raw["occupancy_zones"]["primary_bath"] = {"mine": True}
    out = migrate(raw)[0]
    lights = out["rooms"]["primary_bath"]["lights"]
    assert [x for x in lights if x["id"] == "toilet"] == [
        {"id": "toilet", "set_topic": "mine", "source": "overhead"}
    ]
    assert "bath_front" in out["rooms"]["primary_bath"]["sensors"]
    assert out["occupancy_zones"]["primary_bath"] == {"mine": True}
    raw = _seed_v20()
    raw["rooms"]["primary_bath"]["lights"] = "nope"
    assert migrate(raw)[0]["rooms"]["primary_bath"]["lights"] == "nope"
    raw = _seed_v20()
    del raw["rooms"]["primary_bath"]
    assert "primary_bath" not in migrate(raw)[0]["occupancy_zones"]
    raw = _seed_v20()
    raw["occupancy_zones"] = "nope"
    assert migrate(raw)[0]["occupancy_zones"] == "nope"
