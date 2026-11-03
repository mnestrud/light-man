"""Property tests: the engine never asks for a visible jump between ticks.

"No light jumps" as a number (plan A5): over one glide interval the curve may
move at most 0.2 brightness percentage points per second and 2 mired per second
(a glide of that size is imperceptible as a step). These properties exercise the
real seed curves over random dates, times, house elevations and sleep ramps.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from astral import LocationInfo
from astral.location import Location
from astral.sun import elevation, noon
from hypothesis import given, settings
from hypothesis import strategies as st

from custom_components.light_man import _read_bundled_seed
from custom_components.light_man.adaptive import compute_target, sleep_ramp
from custom_components.light_man.colorspace import xy_to_oklab
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import (
    COLOR_MODE_COLOR_TEMP,
    DEFAULT_SLEEP_RAMP_IN_S,
)
from custom_components.light_man.push import kelvin_to_mired

HOME = Location(LocationInfo("Home", "IL", "America/Chicago", 41.90845, -87.66669))
SOURCES = validate_config(_read_bundled_seed()).config["sources"]
MAX_PCT_PER_S = 0.2
MAX_MIRED_PER_S = 2.0
MAX_OKLAB_PER_S = 0.0015  # cross-mode colour step per second


def _target(profile: dict, when: datetime, sleep_s: float):
    local = when.astimezone(HOME.tzinfo)
    minutes = local.hour * 60 + local.minute + local.second / 60
    e = elevation(HOME.observer, when)
    n = elevation(HOME.observer, noon(HOME.observer, local.date()))
    return compute_target(e, n, profile, sleep_s=sleep_s, now_minutes=minutes)


def _colour_step(a, b) -> tuple[str, float]:
    if a.color_mode == b.color_mode == COLOR_MODE_COLOR_TEMP:
        ma = kelvin_to_mired(a.color_temp_kelvin or 0)
        mb = kelvin_to_mired(b.color_temp_kelvin or 0)
        return "mired", abs(ma - mb)
    if a.xy is not None and b.xy is not None:
        la, lb = xy_to_oklab(a.xy), xy_to_oklab(b.xy)
        return "oklab", sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5
    return "none", 0.0


@settings(max_examples=400, deadline=None)
@given(
    day=st.integers(min_value=0, max_value=364),
    second=st.integers(min_value=0, max_value=86_399),
    interval=st.sampled_from([30, 120]),
    source=st.sampled_from(sorted(SOURCES)),
)
def test_awake_curve_moves_less_than_the_glide_limit(
    day: int, second: int, interval: int, source: str
) -> None:
    profile = SOURCES[source]["profile"]
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=day, seconds=second)
    a = _target(profile, start, 0.0)
    b = _target(profile, start + timedelta(seconds=interval), 0.0)
    assert abs(a.brightness_pct - b.brightness_pct) <= MAX_PCT_PER_S * interval
    kind, step = _colour_step(a, b)
    if kind == "mired":
        assert step <= MAX_MIRED_PER_S * interval


@settings(max_examples=300, deadline=None)
@given(
    elapsed=st.floats(min_value=0, max_value=DEFAULT_SLEEP_RAMP_IN_S),
    hour=st.integers(min_value=17, max_value=23),
    interval=st.sampled_from([30, 120]),
    source=st.sampled_from(sorted(SOURCES)),
)
def test_sleep_ramp_moves_less_than_the_glide_limit(
    elapsed: float, hour: int, interval: int, source: str
) -> None:
    profile = SOURCES[source]["profile"]
    when = datetime(2026, 11, 3, hour, 0, tzinfo=UTC)
    s0 = sleep_ramp(elapsed, DEFAULT_SLEEP_RAMP_IN_S, start=0.0, target=1.0)
    s1 = sleep_ramp(elapsed + interval, DEFAULT_SLEEP_RAMP_IN_S, start=0.0, target=1.0)
    a = _target(profile, when, s0)
    b = _target(profile, when + timedelta(seconds=interval), s1)
    assert abs(a.brightness_pct - b.brightness_pct) <= MAX_PCT_PER_S * interval
    kind, step = _colour_step(a, b)
    if kind == "mired":
        assert step <= MAX_MIRED_PER_S * interval
    elif kind == "oklab":
        assert step <= MAX_OKLAB_PER_S * interval
