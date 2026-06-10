"""Pure adaptive-target engine (Phase 2): real solar elevation -> targets.

Replaces the HACS Adaptive Lighting "dummy" switches. Like :mod:`push`, this is
hass-free and side-effect-free: solar elevation (and today's solar-noon
elevation) are *inputs*, so the entire curve is unit-testable by freezing the
clock + lat/long. The coordinator computes elevation via ``astral`` and feeds it
in; nothing here touches HA, MQTT, or the wall clock.

Model (docs/reference/adaptive-algorithm.md): three elevation regimes
(daytime / twilight / night) produce a **base target**, and a **sleep overlay**
is blended on top by the global sleep toggle's ramp. Color interpolates in
**mired**, brightness in **perceptual** (gamma) space — both fixes for AL's
perceptually-uneven Kelvin/raw-% interpolation.

Not yet here (follow-up commits in the engine work): the optional forced
day-window gate (off by default) and the coordinator wiring/cutover. This module
is the pure math; shadow validation drives it before it replaces the AL read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    COLOR_MODE_COLOR_TEMP,
    COLOR_MODE_RGB,
    DEFAULT_SAT,
    PERCEPTUAL_GAMMA,
    REF_ELEVATION_DEG,
    TWILIGHT_BAND_DEG,
)
from .models import EngineTarget
from .push import kelvin_to_mired, mired_to_kelvin, valid_rgb

if TYPE_CHECKING:
    from .models import SourceProfile


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp ``value`` into ``[lo, hi]``."""
    return max(lo, min(hi, value))


def perceptual_lerp(lo: float, hi: float, t: float) -> float:
    """Interpolate brightness % in perceptual (gamma) space, ``t`` in [0, 1].

    Equal steps in ``t`` feel like equal brightness steps: more luminance
    resolution is spent low (where the eye is most sensitive), unlike a raw-%
    lerp. Endpoints are returned exactly at ``t == 0`` / ``t == 1``.
    """
    g = PERCEPTUAL_GAMMA
    a = lo ** (1.0 / g)
    b = hi ** (1.0 / g)
    return float((a + (b - a) * _clamp(t)) ** g)


def mired_lerp(from_kelvin: float, to_kelvin: float, t: float) -> float:
    """Interpolate color temperature linearly in **mired**, returning mired.

    Reciprocal-to-Kelvin (mired) is perceptually even for white point, so the
    midpoint looks like the midpoint — what a raw-Kelvin lerp gets wrong.
    """
    m0 = kelvin_to_mired(from_kelvin)
    m1 = kelvin_to_mired(to_kelvin)
    return m0 + (m1 - m0) * _clamp(t)


def _regime_brightness(e: float, profile: SourceProfile, noon: float) -> float:
    """Brightness % for elevation ``e`` across the three regimes.

    Daytime saturates by ``sat`` * today's noon (full bright every season);
    twilight winds down to the night floor; night holds the floor.
    """
    min_br = profile["min_br"]
    max_br = profile["max_br"]
    night_floor_br = profile["night_floor_br"]
    if e >= 0:
        sat = profile.get("sat", DEFAULT_SAT)
        denom = sat * noon
        br_pct = _clamp(e / denom) if denom > 0 else 1.0
        return perceptual_lerp(min_br, max_br, br_pct)
    if e >= -TWILIGHT_BAND_DEG:
        tw = _clamp(-e / TWILIGHT_BAND_DEG)
        return perceptual_lerp(min_br, night_floor_br, tw)
    return night_floor_br


def _regime_color_temp_mired(e: float, profile: SourceProfile) -> float:
    """Color temperature (mired) for elevation ``e`` across the three regimes."""
    min_ct = profile["min_ct"]
    if e >= 0:
        return mired_lerp(min_ct, profile["max_ct"], _clamp(e / REF_ELEVATION_DEG))
    if e >= -TWILIGHT_BAND_DEG:
        tw = _clamp(-e / TWILIGHT_BAND_DEG)
        return mired_lerp(min_ct, profile["dusk_floor_ct"], tw)
    return float(kelvin_to_mired(profile["dusk_floor_ct"]))


def base_target(e: float, profile: SourceProfile, noon: float) -> EngineTarget:
    """Return the pre-sleep target for elevation ``e``.

    Brightness always follows the elevation regimes. Color is either the fixed
    configured ``base_rgb`` (when ``base_color_mode == rgb`` — e.g. a daytime
    sky-blue hallway) or the elevation-driven color-temperature curve.
    """
    brightness = _regime_brightness(e, profile, noon)
    base_rgb = profile.get("base_rgb")
    if profile.get("base_color_mode") == COLOR_MODE_RGB and valid_rgb(base_rgb):
        rgb = (int(base_rgb[0]), int(base_rgb[1]), int(base_rgb[2]))  # type: ignore[index]
        return EngineTarget(brightness, COLOR_MODE_RGB, None, rgb)
    mired = _regime_color_temp_mired(e, profile)
    return EngineTarget(brightness, COLOR_MODE_COLOR_TEMP, mired_to_kelvin(mired), None)


def apply_sleep(base: EngineTarget, profile: SourceProfile, s: float) -> EngineTarget:
    """Blend the sleep overlay onto ``base`` by ramp value ``s`` in [0, 1].

    ``s == 0`` returns ``base`` unchanged; ``s == 1`` is the full sleep target.
    Brightness blends perceptually. Color blends within a matching mode (mired
    for ct, per-channel for rgb); a mismatched base/sleep mode (uncommon — real
    sources are ct+ct or rgb+rgb) hard-switches at the midpoint.
    """
    s = _clamp(s)
    if s <= 0:
        return base
    sleep = profile.get("sleep", {})
    brightness = perceptual_lerp(
        base.brightness_pct, sleep.get("br", base.brightness_pct), s
    )
    sleep_mode = sleep.get("color_mode", COLOR_MODE_COLOR_TEMP)
    sleep_rgb = sleep.get("rgb")

    if (
        base.color_mode == COLOR_MODE_RGB
        and sleep_mode == COLOR_MODE_RGB
        and valid_rgb(sleep_rgb)
    ):
        src = base.rgb_color or (0, 0, 0)
        blended = tuple(
            round(c + (sleep_rgb[i] - c) * s)
            for i, c in enumerate(src)  # type: ignore[index]
        )
        return EngineTarget(
            brightness, COLOR_MODE_RGB, None, (blended[0], blended[1], blended[2])
        )

    if base.color_mode == COLOR_MODE_COLOR_TEMP and sleep_mode == COLOR_MODE_COLOR_TEMP:
        base_m = kelvin_to_mired(base.color_temp_kelvin or 0)
        sleep_m = kelvin_to_mired(sleep.get("ct", base.color_temp_kelvin or 0))
        mired = base_m + (sleep_m - base_m) * s
        return EngineTarget(
            brightness, COLOR_MODE_COLOR_TEMP, mired_to_kelvin(mired), None
        )

    # Mismatched modes: keep base color until the midpoint, then the sleep color.
    if s < 0.5:
        return EngineTarget(
            brightness, base.color_mode, base.color_temp_kelvin, base.rgb_color
        )
    if sleep_mode == COLOR_MODE_RGB and valid_rgb(sleep_rgb):
        rgb = (int(sleep_rgb[0]), int(sleep_rgb[1]), int(sleep_rgb[2]))  # type: ignore[index]
        return EngineTarget(brightness, COLOR_MODE_RGB, None, rgb)
    sleep_ct = sleep.get("ct")
    return EngineTarget(brightness, COLOR_MODE_COLOR_TEMP, sleep_ct, None)


def compute_target(
    e: float, noon: float, profile: SourceProfile, *, sleep_s: float = 0.0
) -> EngineTarget:
    """Full per-cycle target: elevation regime + sleep overlay.

    ``e`` is the current solar elevation (deg), ``noon`` today's solar-noon
    elevation (deg, for brightness normalization), ``sleep_s`` the sleep-ramp
    value in [0, 1] (0 = awake). Pure — the only state is its arguments.
    """
    return apply_sleep(base_target(e, profile, noon), profile, sleep_s)


def sleep_ramp(
    elapsed_s: float, duration_s: float, *, start: float, target: float
) -> float:
    """Linear sleep-ramp value at ``elapsed_s`` into a ``duration_s`` transition.

    Ramps from ``start`` (the value held when the toggle last flipped) toward
    ``target`` (1 when going to sleep, 0 when waking). Light Man owns this ramp;
    the user owns the toggle's schedule. A zero/negative duration snaps to target.
    """
    if duration_s <= 0:
        return target
    return start + (target - start) * _clamp(elapsed_s / duration_s)
