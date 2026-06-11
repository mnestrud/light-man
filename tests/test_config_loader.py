"""Unit tests for seed-config validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.light_man.config_loader import validate_config

from .conftest import HALL_OFF, MMWAVE_EAST, SEED


def test_valid_config_builds_switch_map() -> None:
    result = validate_config(copy.deepcopy(SEED))
    assert result.issues == []
    assert result.switch_map["zigbee2mqtt/Living Room Switch"] == (
        "overhead",
        "living_room",
    )
    assert result.config["push_interval_s"] == 30
    assert set(result.config["sources"]) == {"overhead", "hallway_up"}


def test_profile_passes_through_untouched() -> None:
    # The Phase 2 adaptive profile is carried through the loader verbatim (the
    # engine reads it; the loader does not normalize it).
    result = validate_config(copy.deepcopy(SEED))
    profile = result.config["sources"]["overhead"]["profile"]
    assert profile["max_ct"] == 6500
    assert profile["base_color_mode"] == "color_temp"
    assert profile["day_window"]["start"] == "08:00"
    assert profile["sleep"]["ct"] == 2700


def test_occupancy_passes_through() -> None:
    result = validate_config(copy.deepcopy(SEED))
    zone = result.config["occupancy"]["hallway"]
    assert zone["off_lights"] == [HALL_OFF]
    east = zone["sensors"]["east"]
    assert east["topic"] == MMWAVE_EAST
    assert east["occupancy_key"] == "occupancy"  # default when omitted
    assert east["sweep"][0]["lights"][0]["source"] == "hallway_up"
    assert east["sweep"][1]["delay_s"] == 1.0  # the staggered stage's lead-in delay


def test_occupancy_per_sensor_area_keys() -> None:
    # Two sensors on one switch watch distinct mmwave areas independently.
    sensors = validate_config(copy.deepcopy(SEED)).config["occupancy"]["porch"][
        "sensors"
    ]
    assert sensors["porch_a1"]["occupancy_key"] == "area1occupancy"
    assert sensors["porch_a2"]["occupancy_key"] == "area2occupancy"
    assert sensors["porch_a1"]["topic"] == sensors["porch_a2"]["topic"]


def test_occupancy_non_dict_is_soft_issue() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["occupancy"] = "nope"
    result = validate_config(cfg)
    assert result.config["occupancy"] == {}
    assert any("occupancy" in issue for issue in result.issues)


def test_occupancy_bad_zones_dropped_with_issues() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["occupancy"] = {
        "z1": "not-a-mapping",
        "z2": {"sensors": {}},
        "z3": {
            "sensors": {
                "s": {"topic": "t", "sweep": [{"lights": [{"set_topic": "", "s": 1}]}]}
            }
        },
        "z4": {
            "sensors": {
                "s": {
                    "topic": "t",
                    "sweep": [{"lights": [{"set_topic": "x", "source": "ghost"}]}],
                }
            }
        },
        "z5": {"sensors": "not-a-dict"},
        "z6": {"sensors": {"s": {"topic": "t", "sweep": "not-a-list"}}},
        "z7": {"sensors": {"s": {"topic": "t", "sweep": ["bad-stage", {"lights": 5}]}}},
        "z8": {"sensors": {"s": "not-a-mapping"}},
        "z9": {"sensors": {"s": {"topic": "t", "sweep": [{"lights": ["not-a-dict"]}]}}},
        "z10": {
            "sensors": {
                "s": {"sweep": [{"lights": [{"set_topic": "x", "source": "overhead"}]}]}
            }
        },
    }
    result = validate_config(cfg)
    assert result.config["occupancy"] == {}  # every zone invalid
    assert len([i for i in result.issues if "occupancy" in i]) >= 3
    assert any("missing topic" in i for i in result.issues)  # z10


def test_occupancy_defaults_applied() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["occupancy"] = {
        "z": {
            "sensors": {
                "s": {
                    "topic": "t",
                    "sweep": [{"lights": [{"set_topic": "x", "source": "overhead"}]}],
                }
            }
        }
    }
    zone = validate_config(cfg).config["occupancy"]["z"]
    assert zone["sensors"]["s"]["occupancy_key"] == "occupancy"  # default key
    assert zone["off_transition_s"] == 1.5  # default off transition
    assert zone["off_lights"] == []  # none configured


def test_non_dict_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_config("nope")


def test_missing_sources_raises() -> None:
    with pytest.raises(ValueError, match="no 'sources'"):
        validate_config({"push_interval_s": 30})


def test_source_missing_al_switch_raises() -> None:
    bad: dict[str, Any] = {
        "sources": {"x": {"consolidated_topic": "zigbee2mqtt/x/set"}}
    }
    with pytest.raises(ValueError, match="missing 'al_switch'"):
        validate_config(bad)


def test_source_missing_consolidated_topic_raises() -> None:
    bad: dict[str, Any] = {"sources": {"x": {"al_switch": "switch.x"}}}
    with pytest.raises(ValueError, match="missing 'consolidated_topic'"):
        validate_config(bad)


def test_bad_interval_is_soft_issue() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["push_interval_s"] = -5
    result = validate_config(cfg)
    assert result.config["push_interval_s"] == 30
    assert any("push_interval_s" in issue for issue in result.issues)


def test_unknown_color_mode_is_soft_issue() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["sources"]["overhead"]["day_color_mode"] = "rainbow"
    result = validate_config(cfg)
    assert result.config["sources"]["overhead"]["day_color_mode"] == "color_temp"
    assert any("rainbow" in issue for issue in result.issues)


def test_holdable_room_without_set_topic_is_flagged() -> None:
    cfg = copy.deepcopy(SEED)
    del cfg["sources"]["overhead"]["rooms"]["living_room"]["set_topic"]
    result = validate_config(cfg)
    assert any("holdable room has no set_topic" in issue for issue in result.issues)


def test_non_holdable_room_without_set_topic_is_milder_issue() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["sources"]["overhead"]["rooms"]["spare"] = {"switches": []}
    result = validate_config(cfg)
    assert any(issue.endswith("room has no set_topic") for issue in result.issues)


def test_duplicate_switch_is_flagged() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["sources"]["overhead"]["rooms"]["kitchen"]["switches"] = [
        "zigbee2mqtt/Living Room Switch"
    ]
    result = validate_config(cfg)
    assert any("maps to both" in issue for issue in result.issues)


def test_rooms_not_mapping_raises() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["sources"]["overhead"]["rooms"] = ["not", "a", "map"]
    with pytest.raises(ValueError, match="'rooms' must be a mapping"):
        validate_config(cfg)


def test_room_not_mapping_raises() -> None:
    cfg = copy.deepcopy(SEED)
    cfg["sources"]["overhead"]["rooms"]["living_room"] = "nope"
    with pytest.raises(ValueError, match="room must be a mapping"):
        validate_config(cfg)


def test_source_not_mapping_raises() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_config({"sources": {"x": "not-a-dict"}})
