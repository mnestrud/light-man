"""Simulate the Phase-2 adaptive engine across a full day, per source.

Standalone (no HA running): loads the seeded profiles from ``light_man_config.json``,
computes real solar elevation at the house lat/long via ``astral``, and prints
each source's engine target every 30 min for a date. Lets you eyeball the whole
day's curve — shape, timing, seasonal swing, day-window edges — before deploying.

    python scripts/adaptive_sim.py              # today
    python scripts/adaptive_sim.py 2026-12-21   # winter solstice

The sleep overlay is toggle-driven at runtime, so the sim shows the awake curve
(sleep_s = 0). Color is the engine's own decision: ``NNNNK`` for color-temp
sources, ``rgb(r,g,b)`` for rgb sources.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.location import Location
from astral.sun import elevation as solar_elevation
from astral.sun import noon as solar_noon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.light_man.adaptive import compute_target

# House location (docs/reference/adaptive-algorithm.md).
TZ = ZoneInfo("America/Chicago")
HOME = Location(LocationInfo("Home", "IL", "America/Chicago", 41.90845, -87.66669))
CONFIG = (
    Path(__file__).resolve().parents[1]
    / "custom_components/light_man/light_man_config.json"
)


def _describe(target: object) -> str:
    """One-cell description of an engine target."""
    t = target
    if t.color_mode == "rgb":  # type: ignore[attr-defined]
        return f"{t.brightness_pct:3.0f}% rgb{tuple(t.rgb_color)}"  # type: ignore[attr-defined]
    return f"{t.brightness_pct:3.0f}% {t.color_temp_kelvin:>4}K"  # type: ignore[attr-defined]


def main() -> None:
    """Print the per-source day curve for the given (or current) date."""
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    day = (
        datetime.strptime(date_arg, "%Y-%m-%d").replace(tzinfo=TZ).date()
        if date_arg
        else datetime.now(TZ).date()
    )
    sources = json.loads(CONFIG.read_text())["sources"]
    profiles = {k: v["profile"] for k, v in sources.items() if "profile" in v}

    observer = HOME.observer
    noon_e = solar_elevation(observer, solar_noon(observer, day))
    print(f"# {day}  solar-noon elevation = {noon_e:.1f}deg  (REF = 71.5)")
    print(f"{'time':>5} {'elev':>6}  " + "  ".join(f"{k:>22}" for k in profiles))

    for minutes in range(0, 24 * 60, 30):
        when = datetime(day.year, day.month, day.day, tzinfo=TZ) + timedelta(
            minutes=minutes
        )
        e = solar_elevation(observer, when)
        cells = [
            f"{_describe(compute_target(e, noon_e, prof, now_minutes=minutes)):>22}"
            for prof in profiles.values()
        ]
        print(f"{when.strftime('%H:%M'):>5} {e:6.1f}  " + "  ".join(cells))


if __name__ == "__main__":
    main()
