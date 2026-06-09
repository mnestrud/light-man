"""Unit tests for seed-config validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from custom_components.light_man.config_loader import validate_config

from .conftest import SEED


def test_valid_config_builds_switch_map() -> None:
    result = validate_config(copy.deepcopy(SEED))
    assert result.issues == []
    assert result.switch_map["zigbee2mqtt/Living Room Switch"] == (
        "overhead",
        "living_room",
    )
    assert result.config["push_interval_s"] == 30
    assert set(result.config["sources"]) == {"overhead", "hallway_up"}


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
