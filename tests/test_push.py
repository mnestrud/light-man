"""Unit tests for the pure push logic."""

from __future__ import annotations

from typing import Any

from custom_components.light_man.push import (
    build_payload,
    kelvin_to_mired,
    mired_to_kelvin,
    pct_to_brightness,
    plan_source,
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


LOOKS = {"night": NIGHT, "day": DAY_HOLD, "manual": None}


def _plan(modes: dict[str, str]) -> tuple[str, list[tuple[str, Any, bool]]]:
    addressing, sends = plan_source(
        OVERHEAD,
        adaptive_payload=DAY,
        looks={room: LOOKS[mode] for room, mode in modes.items()},
    )
    return addressing, [(s.topic, s.payload, s.dedup) for s in sends]


def test_plan_consolidated_when_no_holds() -> None:
    assert _plan({}) == (
        "consolidated",
        [("zigbee2mqtt/zgb_overhead_all/set", DAY, True)],
    )


def test_plan_flood_then_reassert_for_a_night_hold() -> None:
    addressing, sends = _plan({"living_room": "night"})
    assert addressing == "reassert"
    assert sends == [
        ("zigbee2mqtt/zgb_overhead_all/set", DAY, True),
        ("zigbee2mqtt/zgb_living_room/set", NIGHT, False),
    ]


def test_plan_reasserts_every_held_room_in_one_unit() -> None:
    addressing, sends = _plan({"living_room": "day", "kitchen": "night"})
    assert addressing == "reassert"
    assert [t for t, _p, _d in sends] == [
        "zigbee2mqtt/zgb_overhead_all/set",
        "zigbee2mqtt/zgb_living_room/set",
        "zigbee2mqtt/zgb_kitchen/set",
    ]


def test_plan_known_manual_look_is_reasserted() -> None:
    manual = {"brightness": 40, "color_temp": 400}
    addressing, sends = plan_source(
        OVERHEAD, adaptive_payload=DAY, looks={"living_room": manual}
    )
    assert addressing == "reassert"
    assert sends[1].payload == manual


def test_plan_unknown_manual_level_falls_back_to_per_room() -> None:
    addressing, sends = _plan({"living_room": "manual"})
    assert addressing == "per_room"
    assert [t for t, _p, _d in sends] == ["zigbee2mqtt/zgb_kitchen/set"]


def test_plan_per_room_fallback_keeps_known_looks() -> None:
    addressing, sends = _plan({"living_room": "manual", "kitchen": "night"})
    assert addressing == "per_room"
    assert sends == [("zigbee2mqtt/zgb_kitchen/set", NIGHT, True)]


def test_plan_ignores_looks_for_rooms_outside_the_source() -> None:
    assert _plan({})[0] == "consolidated"
    addressing, _sends = plan_source(
        OVERHEAD, adaptive_payload=DAY, looks={"not_a_room": NIGHT}
    )
    assert addressing == "consolidated"


def test_plan_skips_rooms_without_a_set_topic() -> None:
    source = {
        "consolidated_topic": "zigbee2mqtt/zgb_x/set",
        "rooms": {"a": {"set_topic": ""}, "b": {"set_topic": "zigbee2mqtt/b/set"}},
    }
    addressing, sends = plan_source(source, adaptive_payload=DAY, looks={"a": NIGHT})
    assert addressing == "reassert"
    assert [s.topic for s in sends] == ["zigbee2mqtt/zgb_x/set"]
    addressing, sends = plan_source(
        source, adaptive_payload=DAY, looks={"a": NIGHT, "b": None}
    )
    assert addressing == "per_room"
    assert sends == []


def test_plan_no_rooms_is_consolidated() -> None:
    source = {"consolidated_topic": "zigbee2mqtt/zgb_hallway_up/set", "rooms": {}}
    addressing, sends = plan_source(source, adaptive_payload=DAY, looks={})
    assert addressing == "consolidated"
    assert [s.topic for s in sends] == ["zigbee2mqtt/zgb_hallway_up/set"]


def test_build_payload_xy() -> None:
    xy = build_payload(
        brightness_pct=50,
        color_temp_kelvin=2700,
        rgb_color=None,
        mode="xy",
        transition=120.0,
        xy=(0.31234, 0.25),
    )
    assert xy["color"] == {"x": 0.3123, "y": 0.25}
    assert "color_temp" not in xy


def test_plan_too_many_reasserts_for_one_unit_falls_back_to_per_room() -> None:
    """N3: 1 flood + held re-asserts over ``max_unit`` → per-room, held deduped."""
    looks = {"living_room": NIGHT, "kitchen": DAY_HOLD}
    addressing, sends = plan_source(
        OVERHEAD, adaptive_payload=DAY, looks=looks, max_unit=2
    )
    assert addressing == "per_room"
    assert all(s.dedup for s in sends)
    assert {s.topic for s in sends} == {
        "zigbee2mqtt/zgb_living_room/set",
        "zigbee2mqtt/zgb_kitchen/set",
    }
