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
    assert any("maps to both" in i for i in result.issues)


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
