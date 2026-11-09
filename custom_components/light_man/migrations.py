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
  v11 -> v12 the shade taps' bar animations move from the HA blueprint into the
            switch entries (``animations``), with the blueprint's values. Added
            only to a shipped switch that has no ``animations`` yet.
  v12 -> v13 the primary bath front switch and the toilet switch (new to Light
            Man) each govern the bath overhead and the under-vanity strip; the
            hallway switches' bars follow the downlights (``bar_follows``).
            Applied only where the stored rooms still have them as shipped.
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


# The blueprint's shade animations as of 2026-11-05: light pink (220), level 50,
# 10 s; slow rising / falling on double taps, pulse (living room: rising /
# falling) on triple taps; the island has no triple-tap scene.
_SHADE = {"color": 220, "level": 50, "duration": 10}
_RISING = {"effect": "slow_rising", **_SHADE}
_FALLING = {"effect": "slow_falling", **_SHADE}
_PULSE = {"effect": "pulse", **_SHADE}
_DOUBLES = {"up_double": _RISING, "down_double": _FALLING}
_WITH_PULSE = {**_DOUBLES, "up_triple": _PULSE, "down_triple": _PULSE}
V12_ANIMATIONS: dict[str, dict[str, dict[str, Any]]] = {
    "zigbee2mqtt/Kitchen Overhead Light Switch": _WITH_PULSE,
    "zigbee2mqtt/Kitchen Island Light Switch": _DOUBLES,
    "zigbee2mqtt/Living Room Overhead Light Switch 1": {
        **_DOUBLES,
        "up_triple": _RISING,
        "down_triple": _FALLING,
    },
    "zigbee2mqtt/Office Smart Switch": _WITH_PULSE,
    "zigbee2mqtt/Michael Closet Overhead Switch": _WITH_PULSE,
    "zigbee2mqtt/Primary Bedroom Overhead Light Switch": _WITH_PULSE,
}


def _v11_to_v12(raw: dict[str, Any]) -> dict[str, Any]:
    for room in (raw.get("rooms") or {}).values():
        switches = room.get("switches") if isinstance(room, dict) else None
        for switch in switches if isinstance(switches, list) else []:
            if not isinstance(switch, dict) or "animations" in switch:
                continue
            animations = V12_ANIMATIONS.get(str(switch.get("topic")))
            if animations is not None:
                switch["animations"] = copy.deepcopy(animations)
    return raw


BATH_FRONT = "zigbee2mqtt/Primary Bath Front Smart Switch"
BATH_TOILET = "zigbee2mqtt/Primary Bath Toilet Smart Switch"
V13_BATH_LIGHTS = ["primary_bath.overhead", "primary_bath.under_vanity"]
V13_HALL_BAR = "hallway.east_down"

# v13 -> v14: curve sends every 4 min with a 4-min fade (was 2 min); an edited
# interval is kept.
PUSH_INTERVAL_KEY = "push_interval_s"
V13_PUSH_INTERVAL_S = 120
V14_PUSH_INTERVAL_S = 240


def _v12_to_v13(raw: dict[str, Any]) -> dict[str, Any]:
    rooms = raw.get("rooms") or {}
    bath = rooms.get("primary_bath")
    switches = bath.get("switches") if isinstance(bath, dict) else None
    if isinstance(switches, list):
        front = next(
            (
                sw
                for sw in switches
                if isinstance(sw, dict) and sw.get("topic") == BATH_FRONT
            ),
            None,
        )
        toilet = any(
            isinstance(sw, dict) and sw.get("topic") == BATH_TOILET for sw in switches
        )
        if front is not None and front.get("governs") == V13_BATH_LIGHTS[0]:
            front["governs"] = list(V13_BATH_LIGHTS)
            if not toilet:
                index = switches.index(front) + 1
                switches.insert(
                    index, {"topic": BATH_TOILET, "governs": list(V13_BATH_LIGHTS)}
                )
    hallway = rooms.get("hallway")
    for switch in (
        hallway.get("switches") if isinstance(hallway, dict) else None
    ) or []:
        if (
            isinstance(switch, dict)
            and switch.get("topic") in (HALL_EAST, HALL_WEST)
            and switch.get("governs") == "hallway"
        ):
            switch.setdefault("bar_follows", V13_HALL_BAR)
    return raw


def _v13_to_v14(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get(PUSH_INTERVAL_KEY) == V13_PUSH_INTERVAL_S:
        raw[PUSH_INTERVAL_KEY] = V14_PUSH_INTERVAL_S
    return raw


STAIR_ROOM = "stairwell"
STAIR_TOPIC = "zigbee2mqtt/Stairwell Top Smart Switch mmwave"
STAIR_TOP = "stairwell_top"
STAIR_LANDING = "stairwell_top_landing"
STAIR_LANDING_KEY = "mmwave_area2_occupancy"
V15_STAIR_ON_TRANSITION_S = 0.5


def _v14_to_v15(raw: dict[str, Any]) -> dict[str, Any]:
    """Stairwell: the top switch's landing area (area 2) is a second sensor.

    Added next to the top sensor (same sweep), and the stairwell zone fades in
    over 0.5 s. Left alone when the stairwell room, its top sensor or the zone
    has been edited away.
    """
    room = raw.get("rooms", {}).get(STAIR_ROOM)
    zone = raw.get("occupancy_zones", {}).get(STAIR_ROOM)
    if not isinstance(room, dict) or not isinstance(zone, dict):
        return raw
    sensors = room.get("sensors")
    bindings = zone.get("sensors")
    top_ref = f"{STAIR_ROOM}.{STAIR_TOP}"
    if not isinstance(sensors, dict) or not isinstance(bindings, dict):
        return raw
    top = sensors.get(STAIR_TOP)
    if (
        not isinstance(top, dict)
        or top.get("topic") != STAIR_TOPIC
        or not isinstance(bindings.get(top_ref), dict)
    ):
        return raw
    sensors.setdefault(
        STAIR_LANDING, {"topic": STAIR_TOPIC, "occupancy_key": STAIR_LANDING_KEY}
    )
    bindings.setdefault(
        f"{STAIR_ROOM}.{STAIR_LANDING}", copy.deepcopy(bindings[top_ref])
    )
    zone.setdefault("on_transition_s", V15_STAIR_ON_TRANSITION_S)
    return raw


V16_STEPS = {"min_s": 30, "max_brightness_pct": 2, "max_mired": 5, "max_xy": 0.005}


def _v15_to_v16(raw: dict[str, Any]) -> dict[str, Any]:
    """Variable curve steps: add the step limits (an existing block is kept)."""
    raw.setdefault("steps", dict(V16_STEPS))
    return raw


V16_DAY_START, V16_EDGE_S = "08:00", 1800.0
V17_DAY_START, V17_EDGE_S = "07:30", 3600.0


def _v16_to_v17(raw: dict[str, Any]) -> dict[str, Any]:
    """Mornings start at 07:30 over a 60 min edge (was 08:00 over 30 min).

    Only a curve whose day window still has both shipped values is changed.
    """
    curves = raw.get("curves")
    if not isinstance(curves, dict):
        return raw
    for curve in curves.values():
        window = curve.get("day_window") if isinstance(curve, dict) else None
        if (
            isinstance(window, dict)
            and window.get("start") == V16_DAY_START
            and window.get("edge_transition_s") == V16_EDGE_S
        ):
            window["start"] = V17_DAY_START
            window["edge_transition_s"] = V17_EDGE_S
    return raw


HALL_ZONE = "hallway"
V18_HALL_ON_TRANSITION_S = 0.75


HALL_WEST_REF = "hallway.hall_west"
HALL_WEST_STAIRS = "hall_west_stairs"
HALL_WEST_STAIRS_KEY = "mmwave_area2_occupancy"


def _v17_to_v18(raw: dict[str, Any]) -> dict[str, Any]:
    """Hallway fade 0.75 s; the stair lights follow the west switch's area 2.

    The hallway zone fades in over 0.75 s (a fade already set is kept). The
    west switch's area 1 now covers its whole half of the hallway, so the
    stairwell zone's west binding moves to a new ``hall_west_stairs`` sensor
    (area 2, the foot of the stairs). Left alone when the hallway room's west
    sensor or the stairwell zone's west binding has been edited away.
    """
    zones = raw.get("occupancy_zones", {})
    zone = zones.get(HALL_ZONE)
    if isinstance(zone, dict):
        zone.setdefault("on_transition_s", V18_HALL_ON_TRANSITION_S)
    room = raw.get("rooms", {}).get(HALL_ZONE)
    stair = zones.get(STAIR_ROOM)
    sensors = room.get("sensors") if isinstance(room, dict) else None
    bindings = stair.get("sensors") if isinstance(stair, dict) else None
    if not isinstance(sensors, dict) or not isinstance(bindings, dict):
        return raw
    west = sensors.get("hall_west")
    if not isinstance(west, dict) or HALL_WEST_REF not in bindings:
        return raw
    sensors.setdefault(
        HALL_WEST_STAIRS,
        {"topic": west.get("topic"), "occupancy_key": HALL_WEST_STAIRS_KEY},
    )
    raw["occupancy_zones"][STAIR_ROOM]["sensors"] = {
        (f"{HALL_ZONE}.{HALL_WEST_STAIRS}" if ref == HALL_WEST_REF else ref): bind
        for ref, bind in bindings.items()
    }
    return raw


V19_HALL_ON_TRANSITION_S = 1.0


def _v18_to_v19(raw: dict[str, Any]) -> dict[str, Any]:
    """Hallway zone fades in over 1 s (only a fade still at the shipped 0.75 s)."""
    zone = raw.get("occupancy_zones", {}).get(HALL_ZONE)
    if (
        isinstance(zone, dict)
        and zone.get("on_transition_s") == V18_HALL_ON_TRANSITION_S
    ):
        zone["on_transition_s"] = V19_HALL_ON_TRANSITION_S
    return raw


HALL_PAIR_GROUPS = {
    frozenset({f"hallway.{pos}_up", f"hallway.{pos}_down"}): (
        f"zigbee2mqtt/zgb_hall_{pos}_pair/set"
    )
    for pos in ("east", "center", "west")
}


def _v19_to_v20(raw: dict[str, Any]) -> dict[str, Any]:
    """Hallway sweep stages turn each fixture on with its up+down pair group."""
    zone = raw.get("occupancy_zones", {}).get(HALL_ZONE)
    sensors = zone.get("sensors") if isinstance(zone, dict) else None
    for sensor in sensors.values() if isinstance(sensors, dict) else ():
        sweep = sensor.get("sweep") if isinstance(sensor, dict) else None
        for stage in sweep if isinstance(sweep, list) else ():
            if not isinstance(stage, dict) or "group" in stage:
                continue
            lights = stage.get("lights")
            if not isinstance(lights, list):
                continue
            group = HALL_PAIR_GROUPS.get(frozenset(map(str, lights)))
            if group is not None:
                stage["group"] = group
    return raw


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    8: _v8_to_v9,
    9: _v9_to_v10,
    10: _v10_to_v11,
    11: _v11_to_v12,
    12: _v12_to_v13,
    13: _v13_to_v14,
    14: _v14_to_v15,
    15: _v15_to_v16,
    16: _v16_to_v17,
    17: _v17_to_v18,
    18: _v18_to_v19,
    19: _v19_to_v20,
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
