"""Tests for the pure adaptive-target engine (Phase 2).

The engine takes solar elevation as an input, so a full day/season is a cheap
**simulated sweep** — no live HA, no waiting a real day. We assert the arc's
shape, timing, seasonal swing, regime continuity, and the sleep overlay against
the spec (docs/reference/adaptive-algorithm.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.light_man.adaptive import (
    base_target,
    compute_target,
    mired_lerp,
    perceptual_lerp,
    sleep_ramp,
)
from custom_components.light_man.const import (
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_RGB,
    REF_ELEVATION_DEG,
)

if TYPE_CHECKING:
    from custom_components.light_man.models import SourceProfile

# Real house solar-noon elevations (docs/reference/adaptive-algorithm.md).
SUMMER_NOON = 71.5
EQUINOX_NOON = 48.1
WINTER_NOON = 24.6

OVERHEAD: SourceProfile = {
    "min_br": 30,
    "max_br": 90,
    "min_ct": 2700,
    "max_ct": 6500,
    "sat": 0.5,
    "base_color_mode": COLOR_MODE_COLOR_TEMP,
    "base_rgb": None,
    "dusk_floor_ct": 2200,
    "night_floor_br": 10,
    "sleep": {"br": 30, "color_mode": COLOR_MODE_COLOR_TEMP, "ct": 2200},
}

HALLWAY: SourceProfile = {
    "min_br": 20,
    "max_br": 80,
    "min_ct": 2700,
    "max_ct": 6500,
    "sat": 0.5,
    "base_color_mode": COLOR_MODE_RGB,
    "base_rgb": [80, 160, 255],
    "dusk_floor_ct": 2200,
    "night_floor_br": 15,
    "sleep": {"br": 15, "color_mode": COLOR_MODE_RGB, "rgb": [120, 40, 10]},
}


# --- math primitives --------------------------------------------------------


def test_perceptual_lerp_hits_endpoints() -> None:
    assert perceptual_lerp(30, 90, 0) == pytest.approx(30)
    assert perceptual_lerp(30, 90, 1) == pytest.approx(90)


def test_perceptual_lerp_is_below_linear_midpoint() -> None:
    # Gamma weighting spends resolution low: the perceptual midpoint sits below
    # the arithmetic mean of the endpoints.
    assert perceptual_lerp(0, 100, 0.5) < 50


def test_perceptual_lerp_clamps() -> None:
    assert perceptual_lerp(30, 90, -1) == pytest.approx(30)
    assert perceptual_lerp(30, 90, 2) == pytest.approx(90)


def test_mired_lerp_endpoints_and_monotonic() -> None:
    # mired falls as we go cooler (2700 -> 6500 K).
    warm = mired_lerp(2700, 6500, 0)
    cool = mired_lerp(2700, 6500, 1)
    assert warm > cool
    assert mired_lerp(2700, 6500, 0.5) == pytest.approx((warm + cool) / 2)


# --- daytime regime (1) -----------------------------------------------------


def test_summer_noon_is_max_brightness_and_coolest() -> None:
    t = compute_target(SUMMER_NOON, SUMMER_NOON, OVERHEAD)
    assert t.color_mode == COLOR_MODE_COLOR_TEMP
    assert t.brightness_pct == pytest.approx(90, abs=0.5)  # saturated
    assert t.color_temp_kelvin == pytest.approx(6500, abs=60)  # mired round-trip


def test_horizon_is_warm_floor_brightness() -> None:
    t = compute_target(0.0, SUMMER_NOON, OVERHEAD)
    assert t.color_temp_kelvin == pytest.approx(2700, abs=15)
    assert t.brightness_pct == pytest.approx(30, abs=0.5)


def test_brightness_saturates_by_sat_fraction_of_noon() -> None:
    # sat=0.5 -> full brightness by half of today's noon elevation, and stays.
    knee = 0.5 * SUMMER_NOON
    assert compute_target(knee, SUMMER_NOON, OVERHEAD).brightness_pct == pytest.approx(
        90, abs=0.5
    )
    assert compute_target(
        knee + 15, SUMMER_NOON, OVERHEAD
    ).brightness_pct == pytest.approx(90, abs=0.5)


def test_color_temp_monotonic_in_elevation() -> None:
    temps = [
        base_target(e, OVERHEAD, SUMMER_NOON).color_temp_kelvin for e in range(0, 72)
    ]
    assert temps == sorted(temps)  # cooler as the sun climbs


def test_seasonal_honesty_winter_is_warmer_than_summer() -> None:
    # Fixed REF means winter noon never reaches max_ct — daylight stays warmer.
    summer = base_target(SUMMER_NOON, OVERHEAD, SUMMER_NOON).color_temp_kelvin
    equinox = base_target(EQUINOX_NOON, OVERHEAD, EQUINOX_NOON).color_temp_kelvin
    winter = base_target(WINTER_NOON, OVERHEAD, WINTER_NOON).color_temp_kelvin
    assert winter is not None and equinox is not None and summer is not None
    assert winter < equinox < summer
    assert winter < 4000  # genuinely warm winter midday
    assert summer > 6000  # full cool summer midday


# --- twilight (2) + night (3) ----------------------------------------------


def test_twilight_winds_down_between_floors() -> None:
    t = compute_target(-9.0, SUMMER_NOON, OVERHEAD)  # mid astronomical twilight
    assert 2200 < t.color_temp_kelvin < 2700
    assert 10 < t.brightness_pct < 30


def test_night_holds_the_dusk_floor() -> None:
    t = compute_target(-30.0, SUMMER_NOON, OVERHEAD)
    assert t.color_temp_kelvin == pytest.approx(2200, abs=15)
    assert t.brightness_pct == pytest.approx(10, abs=0.5)


def test_continuous_across_horizon_boundary() -> None:
    day = compute_target(0.001, SUMMER_NOON, OVERHEAD)
    twi = compute_target(-0.001, SUMMER_NOON, OVERHEAD)
    assert day.brightness_pct == pytest.approx(twi.brightness_pct, abs=0.5)
    assert day.color_temp_kelvin == pytest.approx(twi.color_temp_kelvin, abs=10)


def test_continuous_across_twilight_night_boundary() -> None:
    twi = compute_target(-17.99, SUMMER_NOON, OVERHEAD)
    night = compute_target(-18.01, SUMMER_NOON, OVERHEAD)
    assert twi.brightness_pct == pytest.approx(night.brightness_pct, abs=0.5)
    assert twi.color_temp_kelvin == pytest.approx(night.color_temp_kelvin, abs=10)


# --- rgb base source (hallway) ---------------------------------------------


def test_rgb_base_emits_fixed_color_brightness_still_adapts() -> None:
    high = compute_target(SUMMER_NOON, SUMMER_NOON, HALLWAY)
    low = compute_target(-9.0, SUMMER_NOON, HALLWAY)
    assert high.color_mode == COLOR_MODE_RGB
    assert high.rgb_color == (80, 160, 255)  # fixed day color across regimes
    assert low.rgb_color == (80, 160, 255)
    assert high.brightness_pct > low.brightness_pct  # brightness still elevation-driven


# --- sleep overlay ----------------------------------------------------------


def test_sleep_overlay_ct_blends_to_sleep_target() -> None:
    awake = compute_target(40.0, SUMMER_NOON, OVERHEAD, sleep_s=0.0)
    asleep = compute_target(40.0, SUMMER_NOON, OVERHEAD, sleep_s=1.0)
    mid = compute_target(40.0, SUMMER_NOON, OVERHEAD, sleep_s=0.5)
    assert asleep.brightness_pct == pytest.approx(30, abs=0.5)
    assert asleep.color_temp_kelvin == pytest.approx(2200, abs=15)
    assert asleep.brightness_pct < mid.brightness_pct < awake.brightness_pct


def test_sleep_overlay_rgb_blends_per_channel() -> None:
    asleep = compute_target(40.0, SUMMER_NOON, HALLWAY, sleep_s=1.0)
    mid = compute_target(40.0, SUMMER_NOON, HALLWAY, sleep_s=0.5)
    assert asleep.rgb_color == (120, 40, 10)
    assert mid.color_mode == COLOR_MODE_RGB
    assert mid.rgb_color == (100, 100, 132)  # midpoint of [80,160,255] and [120,40,10]


def test_sleep_s_zero_is_base() -> None:
    base = base_target(40.0, OVERHEAD, SUMMER_NOON)
    overlaid = compute_target(40.0, SUMMER_NOON, OVERHEAD, sleep_s=0.0)
    assert overlaid == base


# --- mismatched base/sleep color modes (uncommon: real sources are same-mode) -

MIXED_CT_BASE: SourceProfile = {
    **OVERHEAD,  # type: ignore[misc]
    "sleep": {"br": 20, "color_mode": COLOR_MODE_RGB, "rgb": [120, 40, 10]},
}
MIXED_RGB_BASE: SourceProfile = {
    **HALLWAY,  # type: ignore[misc]
    "sleep": {"br": 15, "color_mode": COLOR_MODE_COLOR_TEMP, "ct": 2200},
}


def test_mixed_ct_base_keeps_ct_then_switches_to_sleep_rgb() -> None:
    early = compute_target(40.0, SUMMER_NOON, MIXED_CT_BASE, sleep_s=0.3)
    late = compute_target(40.0, SUMMER_NOON, MIXED_CT_BASE, sleep_s=0.7)
    assert early.color_mode == COLOR_MODE_COLOR_TEMP  # base color until midpoint
    assert late.color_mode == COLOR_MODE_RGB
    assert late.rgb_color == (120, 40, 10)


def test_mixed_rgb_base_keeps_rgb_then_switches_to_sleep_ct() -> None:
    early = compute_target(40.0, SUMMER_NOON, MIXED_RGB_BASE, sleep_s=0.3)
    late = compute_target(40.0, SUMMER_NOON, MIXED_RGB_BASE, sleep_s=0.7)
    assert early.color_mode == COLOR_MODE_RGB  # base color until midpoint
    assert late.color_mode == COLOR_MODE_COLOR_TEMP
    assert late.color_temp_kelvin == 2200


# --- sleep ramp helper ------------------------------------------------------


def test_sleep_ramp_linear_and_clamped() -> None:
    assert sleep_ramp(0, 90, start=0, target=1) == pytest.approx(0)
    assert sleep_ramp(45, 90, start=0, target=1) == pytest.approx(0.5)
    assert sleep_ramp(90, 90, start=0, target=1) == pytest.approx(1)
    assert sleep_ramp(200, 90, start=0, target=1) == pytest.approx(1)  # clamps


def test_sleep_ramp_from_interrupted_value() -> None:
    # Waking from a partial ramp: start at the held value, ease back to 0.
    assert sleep_ramp(15, 30, start=0.6, target=0) == pytest.approx(0.3)


def test_sleep_ramp_zero_duration_snaps() -> None:
    assert sleep_ramp(5, 0, start=0, target=1) == 1


# --- full simulated day sweep ----------------------------------------------


@pytest.mark.parametrize("noon", [SUMMER_NOON, EQUINOX_NOON, WINTER_NOON])
def test_day_sweep_stays_in_bounds_every_season(noon: float) -> None:
    # Sweep elevation from deep night up past noon and back; every sample must
    # stay within the profile's configured brightness/ct envelope.
    elevations = [e / 10 for e in range(-300, int(noon * 10) + 1, 5)]
    for e in elevations:
        t = compute_target(e, noon, OVERHEAD)
        assert (
            OVERHEAD["night_floor_br"] - 0.5
            <= t.brightness_pct
            <= OVERHEAD["max_br"] + 0.5
        )
        assert t.color_temp_kelvin is not None
        assert (
            OVERHEAD["dusk_floor_ct"] - 60
            <= t.color_temp_kelvin
            <= OVERHEAD["max_ct"] + 60
        )


def test_ref_elevation_is_the_fixed_color_reference() -> None:
    # At REF the daytime color hits max_ct regardless of today's noon (fixed ref).
    t = base_target(REF_ELEVATION_DEG, OVERHEAD, WINTER_NOON)
    assert t.color_temp_kelvin == pytest.approx(6500, abs=60)
