"""Tests for the solar-elevation seam.

Patches HA's ``get_astral_location`` to a real ``astral`` Location at the house
lat/long, so this exercises the actual astral API (verifying signatures) while
staying offline and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from astral import LocationInfo
from astral.location import Location

from custom_components.light_man.solar import day_profile, solar_inputs

# House location (docs/reference/adaptive-algorithm.md).
HOME = Location(LocationInfo("Home", "IL", "America/Chicago", 41.90845, -87.66669))


def _patched_location() -> object:
    return patch(
        "custom_components.light_man.solar.get_astral_location",
        return_value=(HOME, 181.0),
    )


def test_summer_noon_elevation_matches_house() -> None:
    # ~17:30 UTC on the solstice ≈ local solar noon in Chicago (CDT = UTC-5).
    now = datetime(2026, 6, 21, 17, 30, tzinfo=UTC)
    with _patched_location():
        current, noon = solar_inputs(MagicMock(), now)
    assert noon == pytest.approx(71.5, abs=1.5)
    assert current == pytest.approx(noon, abs=5)  # sampled near local noon


def test_winter_noon_elevation_matches_house() -> None:
    now = datetime(2026, 12, 21, 18, 0, tzinfo=UTC)
    with _patched_location():
        _current, noon = solar_inputs(MagicMock(), now)
    assert noon == pytest.approx(24.6, abs=1.5)


def test_noon_is_the_daily_max_even_at_night() -> None:
    now = datetime(2026, 6, 21, 6, 0, tzinfo=UTC)  # ~01:00 local — deep night
    with _patched_location():
        current, noon = solar_inputs(MagicMock(), now)
    assert current < 0  # sun below the horizon
    assert noon > current
    assert noon == pytest.approx(71.5, abs=1.5)


def test_day_profile_samples_the_whole_day_with_events() -> None:
    now = datetime(2026, 6, 21, 17, 30, tzinfo=UTC)
    with _patched_location():
        profile = day_profile(MagicMock(), now, step_minutes=30)
    assert len(profile["samples"]) == 48  # 24h / 30min
    assert profile["noon_elevation"] == pytest.approx(71.5, abs=1.5)
    elevations = [e for _m, e in profile["samples"]]
    assert max(elevations) > 60  # a daytime peak exists
    assert min(elevations) < 0  # and a night trough
    # The summer day has its key sun events, each a valid clock minute.
    assert {"sunrise", "noon", "sunset"} <= set(profile["events"])
    assert all(0 <= m < 1440 for m in profile["events"].values())


def test_day_profile_polar_edge_has_no_events() -> None:
    """When the sun never rises/sets, events are empty but samples still draw."""
    now = datetime(2026, 6, 21, 17, 30, tzinfo=UTC)
    with (
        _patched_location(),
        patch(
            "custom_components.light_man.solar.solar_sun",
            side_effect=ValueError("no sunset"),
        ),
    ):
        profile = day_profile(MagicMock(), now, step_minutes=60)
    assert profile["events"] == {}
    assert len(profile["samples"]) == 24
