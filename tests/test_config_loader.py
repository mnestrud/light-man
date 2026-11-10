"""Unit tests for stored-data-model validation + runtime derivation."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.light_man.config_loader import validate_config

from .conftest import (
    HALL_OFF,
    HALL_UP,
    LR_SET,
    LR_SWITCH,
    MMWAVE_EAST,
    SEED,
)


def _seed() -> dict[str, Any]:
    return copy.deepcopy(SEED)


# --- happy path: runtime + stored derivation --------------------------------


def test_valid_config_builds_switch_map() -> None:
    result = validate_config(_seed())
    assert result.issues == []
    # A switch resolves to (source_group, governed light ref).
    assert result.switch_map[LR_SWITCH] == ("overhead", "living_room.overhead")
    assert result.config["push_interval_s"] == 30
    assert set(result.config["sources"]) == {"overhead", "hallway_up"}


def test_curve_resolves_into_runtime_profile() -> None:
    # The named curve is lifted into each source group's runtime ``profile``.
    result = validate_config(_seed())
    profile = result.config["sources"]["overhead"]["profile"]
    assert profile["max_ct"] == 6500
    assert profile["base_color_mode"] == "color_temp"
    assert profile["sleep"]["ct"] == 2700


def test_runtime_members_keyed_by_light_ref() -> None:
    rooms = validate_config(_seed()).config["sources"]["overhead"]["rooms"]
    assert rooms["living_room.overhead"]["set_topic"] == LR_SET
    assert rooms["living_room.overhead"]["switches"] == [LR_SWITCH]
    # Sweep-only fixtures (porch) are members too, with no governing switch.
    assert rooms["porch.a1"]["switches"] == []


def test_occupancy_resolves_to_runtime_sweep() -> None:
    zone = validate_config(_seed()).config["occupancy"]["hallway"]
    assert zone["off_lights"] == [HALL_OFF]
    east = zone["sensors"]["east"]  # runtime name = the sensor's local segment
    assert east["topic"] == MMWAVE_EAST
    assert east["occupancy_key"] == "occupancy"  # default when omitted
    assert east["sweep"][0]["lights"][0]["source"] == "hallway_up"
    assert east["sweep"][0]["lights"][0]["set_topic"] == HALL_UP
    assert east["sweep"][1]["delay_s"] == 1.0  # the staggered stage's lead-in delay


def test_occupancy_per_sensor_area_keys() -> None:
    sensors = validate_config(_seed()).config["occupancy"]["porch"]["sensors"]
    assert sensors["porch_a1"]["occupancy_key"] == "mmwave_area1_occupancy"
    assert sensors["porch_a2"]["occupancy_key"] == "mmwave_area2_occupancy"
    assert sensors["porch_a1"]["topic"] == sensors["porch_a2"]["topic"]


def test_stored_shape_is_the_data_model() -> None:
    stored = validate_config(_seed()).stored
    assert set(stored["curves"]) == {"standard", "sky"}
    assert stored["sources"]["overhead"]["curve_ref"] == "standard"
    assert len(stored["rooms"]["kitchen"]["lights"]) == 1
    assert "hall.east" in stored["occupancy_zones"]["hallway"]["sensors"]
    assert stored["sleep"]["ramp_in_s"] == 5400


# --- structural errors (raise -> ConfigEntryNotReady) -----------------------


def test_non_dict_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_config("nope")


def test_missing_sources_raises() -> None:
    with pytest.raises(ValueError, match="no 'sources'"):
        validate_config({"push_interval_s": 30})


def test_curves_not_mapping_raises() -> None:
    cfg = _seed()
    cfg["curves"] = "nope"
    with pytest.raises(ValueError, match="'curves' must be a mapping"):
        validate_config(cfg)


def test_source_not_mapping_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_config({"sources": {"x": "not-a-dict"}})


def test_source_missing_consolidated_topic_raises() -> None:
    cfg = _seed()
    del cfg["sources"]["overhead"]["consolidated_topic"]
    with pytest.raises(ValueError, match="missing 'consolidated_topic'"):
        validate_config(cfg)


def test_source_missing_curve_ref_raises() -> None:
    cfg = _seed()
    del cfg["sources"]["overhead"]["curve_ref"]
    with pytest.raises(ValueError, match="missing 'curve_ref'"):
        validate_config(cfg)


def test_source_unknown_curve_raises() -> None:
    cfg = _seed()
    cfg["sources"]["overhead"]["curve_ref"] = "ghost"
    with pytest.raises(ValueError, match="unknown curve 'ghost'"):
        validate_config(cfg)


def test_rooms_not_mapping_raises() -> None:
    cfg = _seed()
    cfg["rooms"] = ["not", "a", "map"]
    with pytest.raises(ValueError, match="'rooms' must be a mapping"):
        validate_config(cfg)


def test_room_not_mapping_raises() -> None:
    cfg = _seed()
    cfg["rooms"]["living_room"] = "nope"
    with pytest.raises(ValueError, match="room 'living_room' must be a mapping"):
        validate_config(cfg)


# --- soft issues (collected, never fatal) -----------------------------------


def test_source_transition_is_carried() -> None:
    cfg = _seed()
    cfg["sources"]["overhead"]["transition_s"] = 2.5
    result = validate_config(cfg)
    assert result.config["sources"]["overhead"]["transition_s"] == 2.5


def test_bad_interval_is_soft_issue() -> None:
    cfg = _seed()
    cfg["push_interval_s"] = -5
    result = validate_config(cfg)
    assert result.config["push_interval_s"] == 30
    assert any("push_interval_s" in issue for issue in result.issues)


def test_curve_non_mapping_entry_is_soft_issue() -> None:
    cfg = _seed()
    cfg["curves"]["junk"] = "nope"
    result = validate_config(cfg)
    assert any("curve 'junk'" in issue for issue in result.issues)


def test_holdable_light_without_set_topic_is_flagged() -> None:
    cfg = _seed()
    del cfg["rooms"]["living_room"]["lights"][0]["set_topic"]
    result = validate_config(cfg)
    assert any("holdable light has no set_topic" in i for i in result.issues)


def test_non_holdable_light_without_set_topic_is_milder() -> None:
    cfg = _seed()
    cfg["rooms"]["hall"]["lights"].append({"id": "spare", "source": "hallway_up"})
    result = validate_config(cfg)
    assert any(i.endswith("light has no set_topic") for i in result.issues)


def test_light_missing_id_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["hall"]["lights"].append({"set_topic": "x", "source": "hallway_up"})
    result = validate_config(cfg)
    assert any("light missing id" in i for i in result.issues)


def test_light_unknown_source_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["hall"]["lights"].append(
        {"id": "z", "set_topic": "x", "source": "ghost"}
    )
    result = validate_config(cfg)
    assert any("unknown source 'ghost'" in i for i in result.issues)


def test_duplicate_switch_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["kitchen"]["switches"][0]["topic"] = LR_SWITCH
    result = validate_config(cfg)
    assert any("listed twice" in i for i in result.issues)


def test_switch_governs_a_list_of_lights() -> None:
    cfg = _seed()
    switch = {"topic": "zigbee2mqtt/Porch Switch", "governs": ["porch.a1", "porch.a2"]}
    cfg["rooms"]["porch"]["switches"] = [switch]
    result = validate_config(cfg)
    assert result.issues == []
    pair = [("overhead", "porch.a1"), ("overhead", "porch.a2")]
    assert result.switch_lights["zigbee2mqtt/Porch Switch"] == pair
    assert result.switch_map["zigbee2mqtt/Porch Switch"] == pair[0]
    assert result.bar_lights["zigbee2mqtt/Porch Switch"] == pair[0]
    rooms = result.config["sources"]["overhead"]["rooms"]
    assert rooms["porch.a2"]["switches"] == ["zigbee2mqtt/Porch Switch"]
    assert result.stored["rooms"]["porch"]["switches"][0]["governs"] == [
        "porch.a1",
        "porch.a2",
    ]


def test_bar_follows_picks_the_bar_light() -> None:
    cfg = _seed()
    cfg["rooms"]["porch"]["switches"] = [
        {
            "topic": "zigbee2mqtt/Porch Switch",
            "governs": ["porch.a1", "porch.a2"],
            "bar_follows": "porch.a2",
        },
        {
            "topic": "zigbee2mqtt/Porch Room",
            "governs": "porch",
            "bar_follows": "porch.a2",
        },
    ]
    result = validate_config(cfg)
    assert result.issues == []
    assert result.switch_map["zigbee2mqtt/Porch Switch"] == ("overhead", "porch.a2")
    assert result.bar_lights["zigbee2mqtt/Porch Room"] == ("overhead", "porch.a2")
    stored = result.stored["rooms"]["porch"]["switches"][0]
    assert stored["bar_follows"] == "porch.a2"


@pytest.mark.parametrize(
    ("entry", "issue"),
    [
        ({"governs": ["porch.a1", 3]}, "must hold light refs"),
        ({"governs": ["porch.a1", "porch.ghost"]}, "governs unknown light"),
        ({"governs": ["porch.a1", "porch.a1"]}, "governs a light twice"),
        ({"governs": "porch.a1", "bar_follows": 7}, "'bar_follows' must be"),
        ({"governs": "porch.a1", "bar_follows": "porch.a2"}, "is not a light it"),
    ],
)
def test_bad_switch_lists_are_flagged(entry: dict[str, Any], issue: str) -> None:
    cfg = _seed()
    cfg["rooms"]["porch"]["switches"] = [{"topic": "zigbee2mqtt/Porch Switch", **entry}]
    result = validate_config(cfg)
    assert any(issue in i for i in result.issues), result.issues


def test_switch_governs_unknown_light_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["living_room"]["switches"][0]["governs"] = "living_room.ghost"
    result = validate_config(cfg)
    assert any("governs unknown light" in i for i in result.issues)


def test_switch_missing_governs_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["living_room"]["switches"][0].pop("governs")
    result = validate_config(cfg)
    assert any("missing 'governs'" in i for i in result.issues)


def test_malformed_room_entries_are_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["hall"]["lights"].append("not-a-dict")
    cfg["rooms"]["living_room"]["switches"].append("not-a-dict")
    cfg["rooms"]["kitchen"]["switches"].append({"governs": "kitchen.overhead"})
    cfg["rooms"]["hall"]["sensors"]["bad"] = "not-a-dict"
    cfg["rooms"]["hall"]["sensors"]["no_topic"] = {"occupancy_key": "occupancy"}
    issues = validate_config(cfg).issues
    assert any("light must be a mapping" in i for i in issues)
    assert any("switch must be a mapping" in i for i in issues)
    assert any("switch missing topic" in i for i in issues)
    assert any("sensor must be a mapping" in i for i in issues)
    assert any("sensor missing topic" in i for i in issues)


def test_room_collection_type_issues() -> None:
    cfg = _seed()
    cfg["rooms"]["living_room"]["lights"] = "nope"
    cfg["rooms"]["kitchen"]["switches"] = "nope"
    cfg["rooms"]["hall"]["sensors"] = "nope"
    issues = validate_config(cfg).issues
    assert any("'lights' must be a list" in i for i in issues)
    assert any("'switches' must be a list" in i for i in issues)
    assert any("'sensors' must be a mapping" in i for i in issues)


# --- occupancy zones (soft) -------------------------------------------------


def test_occupancy_zones_non_dict_is_soft_issue() -> None:
    cfg = _seed()
    cfg["occupancy_zones"] = "nope"
    result = validate_config(cfg)
    assert result.config["occupancy"] == {}
    assert any("occupancy_zones" in i for i in result.issues)


def test_occupancy_defaults_applied() -> None:
    cfg = _seed()
    cfg["occupancy_zones"] = {
        "z": {"sensors": {"hall.east": {"sweep": [{"lights": ["hall.up_main"]}]}}}
    }
    zone = validate_config(cfg).config["occupancy"]["z"]
    assert zone["off_transition_s"] == 1.5  # default off transition
    assert zone["off_lights"] == []  # none configured
    assert "on_transition_s" not in zone  # the sweep uses the default fade-in


@pytest.mark.parametrize(
    ("raw", "expected", "issue"),
    [(0.5, 0.5, False), (0, 0.0, False), (-1, None, True), ("fast", None, True)],
)
def test_occupancy_on_transition(raw: Any, expected: Any, issue: bool) -> None:
    cfg = _seed()
    cfg["occupancy_zones"] = {
        "z": {
            "sensors": {"hall.east": {"sweep": [{"lights": ["hall.up_main"]}]}},
            "on_transition_s": raw,
            "off_transition_s": "slow",  # not a number: the default
        }
    }
    result = validate_config(cfg)
    zone = result.config["occupancy"]["z"]
    assert zone.get("on_transition_s") == expected
    assert zone["off_transition_s"] == 1.5
    assert any("on_transition_s" in i for i in result.issues) is issue


def test_occupancy_bad_zones_dropped_with_issues() -> None:
    cfg = _seed()
    cfg["occupancy_zones"] = {
        "z1": "not-a-mapping",
        "z2": {"sensors": {}},
        "z3": {"sensors": {"hall.ghost": {"sweep": [{"lights": ["hall.up_main"]}]}}},
        "z4": {"sensors": {"hall.east": {"sweep": [{"lights": ["hall.ghost"]}]}}},
        "z5": {"sensors": {"hall.east": "not-a-dict"}},
        "z6": {"sensors": {"hall.east": {"sweep": "not-a-list"}}},
        "z7": {"sensors": {"hall.east": {"sweep": ["bad", {"lights": 5}]}}},
    }
    result = validate_config(cfg)
    assert result.config["occupancy"] == {}  # every zone invalid
    assert any("zone must be a mapping" in i for i in result.issues)  # z1
    assert any("owned by no room" in i for i in result.issues)  # z3 sensor / z4 light


def test_occupancy_duplicate_sensor_name_in_zone_is_flagged() -> None:
    cfg = _seed()
    cfg["rooms"]["garage"] = {
        "name": "Garage",
        "lights": [],
        "switches": [],
        "sensors": {"east": {"topic": "zigbee2mqtt/Garage mmwave"}},
    }
    # Two refs ("hall.east", "garage.east") collapse to the same local name "east".
    cfg["occupancy_zones"]["dup"] = {
        "sensors": {
            "hall.east": {"sweep": [{"lights": ["hall.up_main"]}]},
            "garage.east": {"sweep": [{"lights": ["hall.up_main"]}]},
        }
    }
    result = validate_config(cfg)
    assert any("duplicate sensor name" in i for i in result.issues)


def test_steps_default_when_absent() -> None:
    cfg = _seed()
    cfg.pop("steps", None)
    steps = validate_config(cfg).config["steps"]
    assert steps == {
        "min_s": 30.0,
        "max_brightness_pct": 2.0,
        "max_mired": 5.0,
        "max_xy": 0.005,
    }


def test_steps_bad_values_take_defaults_with_issues() -> None:
    cfg = _seed()
    cfg["steps"] = {"min_s": -1, "max_brightness_pct": "big", "max_mired": 8}
    result = validate_config(cfg)
    steps = result.config["steps"]
    assert steps["min_s"] == 30.0
    assert steps["max_brightness_pct"] == 2.0
    assert steps["max_mired"] == 8.0
    assert sum("steps." in i for i in result.issues) == 2
    cfg["steps"] = "nope"
    assert any("steps: expected a mapping" in i for i in validate_config(cfg).issues)


def test_steps_minimum_above_the_interval_is_clamped() -> None:
    cfg = _seed()
    cfg["steps"] = {"min_s": 1000}
    result = validate_config(cfg)
    assert result.config["steps"]["min_s"] == float(result.config["push_interval_s"])
    assert any("above push_interval_s" in i for i in result.issues)


def test_occupancy_stage_group_is_kept_or_flagged() -> None:
    raw = _seed()
    sweep = raw["occupancy_zones"]["hallway"]["sensors"]["hall.east"]["sweep"]
    sweep[0]["group"] = "zigbee2mqtt/zgb_pair/set"
    sweep[1]["group"] = 7
    result = validate_config(raw)
    east = result.config["occupancy"]["hallway"]["sensors"]["east"]
    assert east["sweep"][0]["group"] == "zigbee2mqtt/zgb_pair/set"
    assert "group" not in east["sweep"][1]
    stored = result.stored["occupancy_zones"]["hallway"]["sensors"]["hall.east"]
    assert stored["sweep"][0]["group"] == "zigbee2mqtt/zgb_pair/set"
    assert any("sweep group 7" in issue for issue in result.issues)


def test_stage_brightness_and_until_off_are_kept_or_flagged() -> None:
    raw = _seed()
    zone = raw["occupancy_zones"]["hallway"]
    sweep = zone["sensors"]["hall.east"]["sweep"]
    sweep[0]["brightness_pct"] = 10
    sweep[1]["brightness_pct"] = 0
    zone["override_switches"] = []
    zone["override_until_off"] = True
    result = validate_config(raw)
    run = result.config["occupancy"]["hallway"]
    assert run["sensors"]["east"]["sweep"][0]["brightness_pct"] == 10.0
    assert "brightness_pct" not in run["sensors"]["east"]["sweep"][1]
    assert run["override_until_off"] is True
    assert result.stored["occupancy_zones"]["hallway"]["override_until_off"] is True
    assert any("bad sweep brightness_pct" in i for i in result.issues)
    zone["override_until_off"] = "yes"
    result = validate_config(raw)
    assert "override_until_off" not in result.config["occupancy"]["hallway"]
    assert any("bad override_until_off" in i for i in result.issues)
