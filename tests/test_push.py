"""Unit tests for the pure push logic."""

from __future__ import annotations

from typing import Any

from custom_components.light_man.push import (
    build_payload,
    kelvin_to_mired,
    mired_to_kelvin,
    pct_to_brightness,
    plan_publishes,
    valid_rgb,
)

OVERHEAD: dict[str, Any] = {
    "consolidated_topic": "zigbee2mqtt/zgb_overhead_all/set",
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
# A distinct night look (the engine supplies this at runtime via night_look()).
NIGHT = build_payload(
    brightness_pct=20,
    color_temp_kelvin=2700,
    rgb_color=None,
    mode="color_temp",
    transition=1.0,
)
# A distinct forced day-look payload (config_double hold), not the live value.
DAY_HOLD = {"brightness": 254, "transition": 1.0, "color_temp": 153}


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


def test_mired_to_kelvin_inverts_and_guards_zero() -> None:
    assert mired_to_kelvin(250) == 4000
    assert mired_to_kelvin(370) == 2703  # round-trips kelvin_to_mired(2700)
    assert mired_to_kelvin(0) == 0  # guard
    assert mired_to_kelvin(-5) == 0


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


def _plan(modes: dict[str, str]) -> list[tuple[str, Any]]:
    return plan_publishes(
        OVERHEAD,
        adaptive_payload=DAY,
        day_payload=DAY_HOLD,
        night_payload=NIGHT,
        modes=modes,
    )


def test_plan_consolidated_when_no_holds() -> None:
    # No held room → one consolidated flood (steady state = 4 floods house-wide).
    assert _plan({}) == [("zigbee2mqtt/zgb_overhead_all/set", DAY)]


def test_plan_per_room_with_a_night_hold() -> None:
    plan = dict(_plan({"living_room": "night"}))
    assert plan["zigbee2mqtt/zgb_living_room/set"] == NIGHT
    assert plan["zigbee2mqtt/zgb_kitchen/set"] == DAY
    assert "zigbee2mqtt/zgb_overhead_all/set" not in plan


def test_plan_per_room_with_a_day_hold() -> None:
    # A day hold is a distinct look, so it forces per-room (not the flood).
    plan = dict(_plan({"living_room": "day"}))
    assert plan["zigbee2mqtt/zgb_living_room/set"] == DAY_HOLD
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
        source,
        adaptive_payload=DAY,
        day_payload=DAY_HOLD,
        night_payload=NIGHT,
        modes={},
    ) == [("zigbee2mqtt/zgb_hallway_up/set", DAY)]
