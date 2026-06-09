"""Unit tests for the pure push logic."""

from __future__ import annotations

from typing import Any

from custom_components.light_man.push import (
    build_night_payload,
    build_payload,
    kelvin_to_mired,
    pct_to_brightness,
    plan_publishes,
    resolve_color_mode,
    valid_rgb,
)

OVERHEAD: dict[str, Any] = {
    "consolidated_topic": "zigbee2mqtt/zgb_overhead_all/set",
    "day_color_mode": "color_temp",
    "night_color_mode": "color_temp",
    "night_brightness_pct": 20,
    "night_color_temp_kelvin": 2700,
    "rooms": {
        "living_room": {"set_topic": "zigbee2mqtt/zgb_living_room/set", "switches": []},
        "kitchen": {"set_topic": "zigbee2mqtt/zgb_kitchen/set", "switches": []},
    },
}
DAY = build_payload(
    brightness_pct=90,
    color_temp_kelvin=5650,
    rgb_color=None,
    mode="color_temp",
    transition=1.0,
)
NIGHT = build_night_payload(OVERHEAD, 1.0)


def test_pct_to_brightness_scales_and_clamps() -> None:
    assert pct_to_brightness(0) == 0
    assert pct_to_brightness(100) == 254
    assert pct_to_brightness(50) == 127
    assert pct_to_brightness(150) == 254


def test_kelvin_to_mired_clamps() -> None:
    assert kelvin_to_mired(4000) == 250
    assert kelvin_to_mired(10000) == 153
    assert kelvin_to_mired(1000) == 500
    assert kelvin_to_mired(0) == 500


def test_resolve_color_mode_by_sleep_state() -> None:
    hall = {"day_color_mode": "color_temp", "night_color_mode": "rgb"}
    assert resolve_color_mode(hall, sleeping=False) == "color_temp"
    assert resolve_color_mode(hall, sleeping=True) == "rgb"
    assert resolve_color_mode({}, sleeping=False) == "color_temp"


def test_valid_rgb() -> None:
    assert valid_rgb([0, 128, 255])
    assert not valid_rgb([1, 2])
    assert not valid_rgb([1, 2, 300])
    assert not valid_rgb(None)


def test_build_payload_color_temp_and_rgb() -> None:
    ct = build_payload(
        brightness_pct=50,
        color_temp_kelvin=4000,
        rgb_color=None,
        mode="color_temp",
        transition=1.0,
    )
    assert ct == {"brightness": 127, "transition": 1.0, "color_temp": 250}
    rgb = build_payload(
        brightness_pct=30,
        color_temp_kelvin=2700,
        rgb_color=[255, 100, 50],
        mode="rgb",
        transition=2.0,
    )
    assert rgb["color"] == {"r": 255, "g": 100, "b": 50}
    assert "color_temp" not in rgb


def test_build_night_payload_color_temp() -> None:
    expected = {
        "brightness": pct_to_brightness(20),
        "transition": 1.0,
        "color_temp": kelvin_to_mired(2700),
    }
    assert expected == NIGHT


def test_build_night_payload_rgb() -> None:
    source = {"night_rgb": [10, 20, 30]}
    payload = build_night_payload(source, 1.0)
    assert payload["color"] == {"r": 10, "g": 20, "b": 30}


def test_build_night_payload_defaults() -> None:
    payload = build_night_payload({}, 1.0)
    assert payload["color_temp"] == kelvin_to_mired(2700)  # default night ct


def _plan(modes: dict[str, str]) -> list[tuple[str, Any]]:
    return plan_publishes(
        OVERHEAD, adaptive_payload=DAY, night_payload=NIGHT, modes=modes
    )


def test_plan_consolidated_when_all_adaptive() -> None:
    # Off rooms are NOT skipped — they stage color-while-off via the flood.
    assert _plan({}) == [("zigbee2mqtt/zgb_overhead_all/set", DAY)]


def test_plan_per_room_with_a_night_hold() -> None:
    plan = dict(_plan({"living_room": "night"}))
    assert plan["zigbee2mqtt/zgb_living_room/set"] == NIGHT
    assert plan["zigbee2mqtt/zgb_kitchen/set"] == DAY
    assert "zigbee2mqtt/zgb_overhead_all/set" not in plan


def test_plan_skips_manual_freeze() -> None:
    topics = [t for t, _ in _plan({"living_room": "manual"})]
    assert topics == ["zigbee2mqtt/zgb_kitchen/set"]


def test_plan_all_manual_publishes_nothing() -> None:
    assert _plan({"living_room": "manual", "kitchen": "manual"}) == []


def test_plan_no_rooms_is_consolidated() -> None:
    source = {"consolidated_topic": "zigbee2mqtt/zgb_hallway_up/set", "rooms": {}}
    assert plan_publishes(
        source, adaptive_payload=DAY, night_payload=NIGHT, modes={}
    ) == [("zigbee2mqtt/zgb_hallway_up/set", DAY)]
