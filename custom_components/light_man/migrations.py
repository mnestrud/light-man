"""In-place migration of the stored config across bundled ``seed_version`` bumps.

The Store (``.storage/light_man_config``) is the live authority. When a release
ships a newer bundled seed, the stored config is **migrated in place** rather than
overwritten, so anything edited in the Store survives. Each step is a pure
function ``vN -> vN+1`` on a deep copy; the loader backs up the old Store first.

Versions:
  v8 -> v9  make implicit curve defaults explicit (``sat``, day-window
            ``edge_transition_s`` / ``wind_down_s``). Engine output is identical.
  v9 -> v10 ``push_interval_s`` 30 -> 120 (bulb-side glide makes the longer
            interval invisible; a value other than the old default is kept).
  v10 -> v11 the hallway east + west switches govern the whole hallway and hold
            its motion off (``override_switches``); the hallway and stairwell
            switches follow their zones (``sync_switches``). Applied only where
            the stored rooms still have those switches as shipped. Behaviour
            change, intended (Workstream F / F-hall).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from .const import (
    BUNDLED_SEED_VERSION,
    CONF_CURVES,
    DEFAULT_EDGE_TRANSITION_S,
    DEFAULT_SAT,
    DEFAULT_WIND_DOWN_S,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SEED_VERSION_KEY = "seed_version"
MIN_MIGRATABLE_VERSION = 8


class MigrationError(ValueError):
    """The stored config cannot be migrated to the bundled version."""


def _v8_to_v9(raw: dict[str, Any]) -> dict[str, Any]:
    for curve in (raw.get(CONF_CURVES) or {}).values():
        if not isinstance(curve, dict):
            continue
        curve.setdefault("sat", DEFAULT_SAT)
        window = curve.get("day_window")
        if isinstance(window, dict):
            window.setdefault("edge_transition_s", DEFAULT_EDGE_TRANSITION_S)
            window.setdefault("wind_down_s", DEFAULT_WIND_DOWN_S)
    return raw


V9_PUSH_INTERVAL_S = 30
V10_PUSH_INTERVAL_S = 120


def _v9_to_v10(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("push_interval_s") == V9_PUSH_INTERVAL_S:
        raw["push_interval_s"] = V10_PUSH_INTERVAL_S
    return raw


HALL_EAST = "zigbee2mqtt/Hallway Light Switch East New New mmwave"
HALL_WEST = "zigbee2mqtt/Hall Smart Switch West New mmwave"
STAIRWELL_TOP = "zigbee2mqtt/Stairwell Top Smart Switch mmwave"


def _v10_to_v11(raw: dict[str, Any]) -> dict[str, Any]:
    rooms = raw.get("rooms") or {}
    zones = raw.get("occupancy_zones") or {}
    hallway = rooms.get("hallway")
    hall_zone = zones.get("hallway")
    if (
        isinstance(hallway, dict)
        and isinstance(hall_zone, dict)
        and not hallway.get("switches")
    ):
        hallway["switches"] = [
            {"topic": HALL_EAST, "governs": "hallway"},
            {"topic": HALL_WEST, "governs": "hallway"},
        ]
        hall_zone.setdefault("sync_switches", [HALL_EAST])
        hall_zone.setdefault("override_switches", [HALL_EAST, HALL_WEST])
        hall_zone.setdefault("override_idle_s", 3600)
    stairwell = rooms.get("stairwell")
    stair_zone = zones.get("stairwell")
    if (
        isinstance(stairwell, dict)
        and isinstance(stair_zone, dict)
        and any(
            isinstance(sw, dict) and sw.get("topic") == STAIRWELL_TOP
            for sw in stairwell.get("switches") or []
        )
    ):
        stair_zone.setdefault("sync_switches", [STAIRWELL_TOP])
    return raw


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    8: _v8_to_v9,
    9: _v9_to_v10,
    10: _v10_to_v11,
}


def stored_version(raw: dict[str, Any]) -> int:
    """Return the stored seed version (0 if absent or not an int)."""
    version = raw.get(SEED_VERSION_KEY, 0)
    return version if isinstance(version, int) else 0


def migrate(
    raw: dict[str, Any], *, target: int = BUNDLED_SEED_VERSION
) -> tuple[dict[str, Any], list[int]]:
    """Return ``(migrated copy, versions stepped through)``; raise MigrationError."""
    version = stored_version(raw)
    if version < MIN_MIGRATABLE_VERSION:
        msg = f"stored seed_version {version} predates v{MIN_MIGRATABLE_VERSION}"
        raise MigrationError(msg)
    migrated = copy.deepcopy(raw)
    steps: list[int] = []
    while version < target:
        step = MIGRATIONS.get(version)
        if step is None:
            msg = f"no migration from seed_version {version}"
            raise MigrationError(msg)
        migrated = step(migrated)
        steps.append(version)
        version += 1
        migrated[SEED_VERSION_KEY] = version
    return migrated, steps
