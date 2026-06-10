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

from typing import TYPE_CHECKING

from astral.sun import elevation as solar_elevation
from astral.sun import noon as solar_noon
from homeassistant.helpers.sun import get_astral_location
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant


def solar_inputs(hass: HomeAssistant, now: datetime) -> tuple[float, float]:
    """Return ``(current_elevation, today_noon_elevation)`` in degrees.

    ``now`` must be tz-aware. ``today_noon_elevation`` is the sun's elevation at
    today's solar noon at the house lat/long — the daily maximum the engine
    normalizes brightness against.
    """
    location, _observer_elevation = get_astral_location(hass)
    observer = location.observer
    current = solar_elevation(observer, now)
    noon_when = solar_noon(observer, dt_util.as_local(now).date())
    noon_elevation = solar_elevation(observer, noon_when)
    return current, noon_elevation
