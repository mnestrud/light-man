"""Solar-elevation inputs for the adaptive engine — the engine's only sky read.

:mod:`adaptive` is pure math on an elevation angle; this thin helper is the one
place that asks "where is the sun right now". It uses HA's bundled ``astral``
(the same library behind the ``sun.sun`` entity — no new dependency), called the
same way HA's own sun integration does: ``astral.sun.elevation(observer, when)``.

Returns the current solar elevation and today's solar-noon elevation (the
brightness normalizer — the engine drives full brightness by ``sat`` * noon
every season). Kept separate from the coordinator so it is unit-testable against
real astral with a patched location.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, TypedDict

from astral.sun import elevation as solar_elevation
from astral.sun import noon as solar_noon
from astral.sun import sun as solar_sun
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import datetime

    from astral import Observer
    from homeassistant.core import HomeAssistant

try:
    # HA 2026.x+ — get_astral_location is deprecated (removed 2027.7).
    # type-ignore: the pinned test-env HA predates this helper; drop the ignore
    # (and this whole shim) when requirements_test.txt moves past it.
    from homeassistant.helpers.sun import get_astral_observer  # type: ignore[attr-defined]
except ImportError:
    from homeassistant.helpers.sun import get_astral_location

    def get_astral_observer(hass: HomeAssistant) -> Observer:
        """Fallback for cores that predate get_astral_observer."""
        return get_astral_location(hass)[0].observer

# Events labelled on the curve viz's time axis (clock minutes since midnight).
_SUN_EVENTS = ("dawn", "sunrise", "noon", "sunset", "dusk")


class DayProfile(TypedDict):
    """Today's sun path, for the panel's time-of-day curve visualization."""

    samples: list[tuple[int, float]]  # (clock minutes since midnight, elevation deg)
    events: dict[str, int]  # event name -> clock minutes (omitted if it doesn't occur)
    noon_elevation: float


def solar_inputs(hass: HomeAssistant, now: datetime) -> tuple[float, float]:
    """Return ``(current_elevation, today_noon_elevation)`` in degrees.

    ``now`` must be tz-aware. ``today_noon_elevation`` is the sun's elevation at
    today's solar noon at the house lat/long — the daily maximum the engine
    normalizes brightness against.
    """
    observer = get_astral_observer(hass)
    current = solar_elevation(observer, now)
    noon_when = solar_noon(observer, dt_util.as_local(now).date())
    noon_elevation = solar_elevation(observer, noon_when)
    return current, noon_elevation


def day_profile(
    hass: HomeAssistant, when: datetime, *, step_minutes: int = 20
) -> DayProfile:
    """Sample today's solar elevation across the clock + the day's sun events.

    Returns the elevation at ``step_minutes`` intervals from local midnight plus
    the clock time of dawn/sunrise/noon/sunset/dusk, so the panel can draw a
    curve's brightness over the *whole* day (rise → peak → sunset wind-down) with
    real time-of-day labels. Pure read of HA's bundled ``astral``.
    """
    observer = get_astral_observer(hass)
    local = dt_util.as_local(when)
    date = local.date()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    samples: list[tuple[int, float]] = []
    minute = 0
    while minute < 24 * 60:
        elevation = solar_elevation(observer, midnight + timedelta(minutes=minute))
        samples.append((minute, round(elevation, 2)))
        minute += step_minutes
    events: dict[str, int] = {}
    try:
        sun_events = solar_sun(observer, date, tzinfo=local.tzinfo)
    except ValueError:
        sun_events = {}  # polar day/night — no events, samples still drawable
    for name in _SUN_EVENTS:
        moment = sun_events.get(name)
        if moment is not None:
            at = dt_util.as_local(moment)
            events[name] = at.hour * 60 + at.minute
    noon_when = solar_noon(observer, date)
    return DayProfile(
        samples=samples,
        events=events,
        noon_elevation=round(solar_elevation(observer, noon_when), 2),
    )
