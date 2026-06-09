"""Unit tests for the pure push logic."""

from __future__ import annotations

from custom_components.light_man.push import (
    build_payload,
    kelvin_to_mired,
    pct_to_brightness,
    resolve_color_mode,
    select_targets,
    valid_rgb,
)

OVERHEAD = {
    "consolidated_topic": "zigbee2mqtt/zgb_overhead_all/set",
    "day_color_mode": "color_temp",
    "night_color_mode": "rgb",
    "rooms": {
        "living_room": {"set_topic": "zigbee2mqtt/zgb_living_room/set", "switches": []},
        "kitchen": {"set_topic": "zigbee2mqtt/zgb_kitchen/set", "switches": []},
    },
}


def test_pct_to_brightness_scales_and_clamps() -> None:
    assert pct_to_brightness(0) == 0
    assert pct_to_brightness(100) == 254
    assert pct_to_brightness(50) == 127
    assert pct_to_brightness(150) == 254
    assert pct_to_brightness(-5) == 0


def test_kelvin_to_mired_clamps_to_device_range() -> None:
    assert kelvin_to_mired(4000) == 250
    assert kelvin_to_mired(10000) == 153  # clamped to MIRED_MIN
    assert kelvin_to_mired(1000) == 500  # clamped to MIRED_MAX
    assert kelvin_to_mired(0) == 500  # non-positive guard


def test_resolve_color_mode_by_sleep_state() -> None:
    assert resolve_color_mode(OVERHEAD, sleeping=False) == "color_temp"
    assert resolve_color_mode(OVERHEAD, sleeping=True) == "rgb"
    assert resolve_color_mode({}, sleeping=False) == "color_temp"  # default


def test_valid_rgb() -> None:
    assert valid_rgb([0, 128, 255])
    assert valid_rgb((1, 2, 3))
    assert not valid_rgb([1, 2])
    assert not valid_rgb([1, 2, 300])
    assert not valid_rgb("ffaa00")
    assert not valid_rgb(None)


def test_build_payload_color_temp() -> None:
    payload = build_payload(
        brightness_pct=50,
        color_temp_kelvin=4000,
        rgb_color=None,
        mode="color_temp",
        transition=1.0,
    )
    assert payload == {"brightness": 127, "transition": 1.0, "color_temp": 250}


def test_build_payload_rgb() -> None:
    payload = build_payload(
        brightness_pct=30,
        color_temp_kelvin=2700,
        rgb_color=[255, 100, 50],
        mode="rgb",
        transition=2.0,
    )
    assert payload["color"] == {"r": 255, "g": 100, "b": 50}
    assert "color_temp" not in payload


def test_build_payload_rgb_invalid_falls_back_to_color_temp() -> None:
    payload = build_payload(
        brightness_pct=30,
        color_temp_kelvin=2700,
        rgb_color=None,  # rgb mode but no valid color
        mode="rgb",
        transition=1.0,
    )
    assert "color" not in payload
    assert payload["color_temp"] == kelvin_to_mired(2700)


def test_select_targets_consolidated_when_nothing_held() -> None:
    assert select_targets(OVERHEAD, set()) == ["zigbee2mqtt/zgb_overhead_all/set"]
    # A held room in a *different* source does not affect this source.
    assert select_targets(OVERHEAD, {"bedroom"}) == ["zigbee2mqtt/zgb_overhead_all/set"]


def test_select_targets_per_room_skips_held() -> None:
    targets = select_targets(OVERHEAD, {"living_room"})
    assert targets == ["zigbee2mqtt/zgb_kitchen/set"]


def test_select_targets_all_rooms_held_returns_empty() -> None:
    assert select_targets(OVERHEAD, {"living_room", "kitchen"}) == []
