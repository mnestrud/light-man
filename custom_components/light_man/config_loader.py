"""Load and validate the Store-seeded topology config.

Structural problems (not a dict, no sources, a source missing its AL switch or
consolidated topic) raise :class:`ValueError` so ``_async_setup`` can turn them
into ``ConfigEntryNotReady``. Softer problems (a holdable room with no per-room
``set_topic``, a switch mapped to two rooms, an unknown color mode) are collected
as issues and surfaced in diagnostics — they never fail setup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .const import (
    COLOR_MODES,
    CONF_AL_SWITCH,
    CONF_CONSOLIDATED_TOPIC,
    CONF_DAY_COLOR_MODE,
    CONF_LIGHTS,
    CONF_NIGHT_COLOR_MODE,
    CONF_OCCUPANCY,
    CONF_OCCUPANCY_KEY,
    CONF_OFF_LIGHTS,
    CONF_OFF_TRANSITION,
    CONF_PUSH_INTERVAL,
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_SET_TOPIC,
    CONF_SOURCE,
    CONF_SOURCES,
    CONF_STAGE_DELAY,
    CONF_SWEEP,
    CONF_SWITCHES,
    CONF_TOPIC,
    DEFAULT_COLOR_MODE,
    DEFAULT_OCCUPANCY_KEY,
    DEFAULT_OCCUPANCY_TRANSITION_S,
    DEFAULT_PUSH_INTERVAL_S,
)
from .models import (
    LightManConfig,
    OccupancyLight,
    OccupancySensor,
    OccupancyStage,
    OccupancyZone,
    RoomConfig,
    SourceConfig,
)


@dataclass(slots=True)
class ValidatedConfig:
    """Result of validating the seed config."""

    config: LightManConfig
    # Inovelli switch base topic -> (source_key, room_key).
    switch_map: dict[str, tuple[str, str]]
    issues: list[str] = field(default_factory=list)


def _validate_room(
    source_key: str,
    room_key: str,
    raw: object,
    issues: list[str],
) -> RoomConfig:
    """Validate one room block; collect soft issues, return a normalized room."""
    if not isinstance(raw, dict):
        msg = f"{source_key}.{room_key}: room must be a mapping"
        raise ValueError(msg)
    set_topic = raw.get(CONF_SET_TOPIC)
    switches_raw = raw.get(CONF_SWITCHES, [])
    switches = (
        [s for s in switches_raw if isinstance(s, str)]
        if isinstance(switches_raw, list)
        else []
    )
    if not isinstance(set_topic, str) or not set_topic:
        # Holdable rooms (those with switches) MUST have a set_topic, else the
        # push cannot address around them and would hit the held bulbs.
        if switches:
            issues.append(
                f"{source_key}.{room_key}: holdable room has no set_topic "
                "(cannot be excluded from a flood)"
            )
        else:
            issues.append(f"{source_key}.{room_key}: room has no set_topic")
        set_topic = ""
    return RoomConfig(set_topic=set_topic, switches=switches)


def _apply_color_modes(
    source_key: str, raw: dict[str, object], source: SourceConfig, issues: list[str]
) -> None:
    """Normalize the day/night color modes onto ``source``, collecting issues."""
    for mode_key in (CONF_DAY_COLOR_MODE, CONF_NIGHT_COLOR_MODE):
        mode = raw.get(mode_key, DEFAULT_COLOR_MODE)
        if mode not in COLOR_MODES:
            issues.append(
                f"{source_key}.{mode_key}: unknown color mode {mode!r}, "
                f"using {DEFAULT_COLOR_MODE!r}"
            )
            mode = DEFAULT_COLOR_MODE
        source[mode_key] = mode


def _validate_rooms(
    source_key: str,
    raw_rooms: object,
    switch_map: dict[str, tuple[str, str]],
    issues: list[str],
) -> dict[str, RoomConfig]:
    """Validate a source's rooms and register their switches in ``switch_map``."""
    if not isinstance(raw_rooms, dict):
        msg = f"source {source_key!r} '{CONF_ROOMS}' must be a mapping"
        raise ValueError(msg)
    rooms: dict[str, RoomConfig] = {}
    for room_key, room_raw in raw_rooms.items():
        room = _validate_room(source_key, room_key, room_raw, issues)
        rooms[room_key] = room
        for base in room["switches"]:
            if base in switch_map:
                issues.append(
                    f"switch {base!r} maps to both {switch_map[base]} and "
                    f"{(source_key, room_key)}"
                )
                continue
            switch_map[base] = (source_key, room_key)
    return rooms


def _validate_source(
    source_key: str,
    raw: object,
    switch_map: dict[str, tuple[str, str]],
    issues: list[str],
) -> SourceConfig:
    """Validate one source block; structural errors raise, soft ones collect."""
    if not isinstance(raw, dict):
        msg = f"source {source_key!r} must be a mapping"
        raise ValueError(msg)
    al_switch = raw.get(CONF_AL_SWITCH)
    consolidated = raw.get(CONF_CONSOLIDATED_TOPIC)
    if not isinstance(al_switch, str) or not al_switch:
        msg = f"source {source_key!r} is missing '{CONF_AL_SWITCH}'"
        raise ValueError(msg)
    if not isinstance(consolidated, str) or not consolidated:
        msg = f"source {source_key!r} is missing '{CONF_CONSOLIDATED_TOPIC}'"
        raise ValueError(msg)

    source: SourceConfig = {
        CONF_AL_SWITCH: al_switch,
        CONF_CONSOLIDATED_TOPIC: consolidated,
    }
    _apply_color_modes(source_key, raw, source, issues)
    for opt in (
        "legacy_enable",
        "sleep_switch",
        "transition_s",
        "night_brightness_pct",
        "night_color_temp_kelvin",
        "night_rgb",
        "profile",
    ):
        if opt in raw:
            source[opt] = raw[opt]
    source[CONF_ROOMS] = _validate_rooms(
        source_key, raw.get(CONF_ROOMS, {}), switch_map, issues
    )
    return source


def _validate_light(
    zone_key: str, raw: object, source_keys: set[str], issues: list[str]
) -> OccupancyLight | None:
    """Validate one sweep light ``{set_topic, source}``; None if invalid."""
    if not isinstance(raw, dict):
        return None
    set_topic = raw.get(CONF_SET_TOPIC)
    source = raw.get(CONF_SOURCE)
    if not isinstance(set_topic, str) or not set_topic:
        issues.append(f"occupancy.{zone_key}: light missing set_topic")
        return None
    if not isinstance(source, str) or source not in source_keys:
        issues.append(f"occupancy.{zone_key}: light source {source!r} unknown")
        return None
    return OccupancyLight(set_topic=set_topic, source=source)


def _validate_sweep(
    zone_key: str, raw_sweep: object, source_keys: set[str], issues: list[str]
) -> list[OccupancyStage]:
    """Validate a sensor's ordered sweep stages (each a delay + its lights)."""
    if not isinstance(raw_sweep, list):
        return []
    stages: list[OccupancyStage] = []
    for raw_stage in raw_sweep:
        if not isinstance(raw_stage, dict):
            continue
        raw_lights = raw_stage.get(CONF_LIGHTS, [])
        lights: list[OccupancyLight] = []
        for raw_light in raw_lights if isinstance(raw_lights, list) else []:
            light = _validate_light(zone_key, raw_light, source_keys, issues)
            if light is not None:
                lights.append(light)
        if not lights:
            continue
        stage: OccupancyStage = {CONF_LIGHTS: lights}
        delay = raw_stage.get(CONF_STAGE_DELAY)
        if isinstance(delay, (int, float)) and not isinstance(delay, bool):
            stage[CONF_STAGE_DELAY] = float(delay)
        stages.append(stage)
    return stages


def _validate_sensor(
    zone_key: str,
    name: str,
    raw: object,
    source_keys: set[str],
    issues: list[str],
) -> OccupancySensor | None:
    """Validate one named sensor ``{topic, occupancy_key?, sweep}``; None if invalid.

    ``occupancy_key`` is per-sensor so two sensors can share a ``topic`` while
    watching different mmwave areas (e.g. ``mmwave_area1_occupancy`` /
    ``mmwave_area2_occupancy``)
    as independent triggers.
    """
    if not isinstance(raw, dict):
        return None
    topic = raw.get(CONF_TOPIC)
    if not isinstance(topic, str) or not topic:
        issues.append(f"occupancy.{zone_key}.{name}: sensor missing topic")
        return None
    sweep = _validate_sweep(zone_key, raw.get(CONF_SWEEP, []), source_keys, issues)
    if not sweep:
        return None
    key = raw.get(CONF_OCCUPANCY_KEY, DEFAULT_OCCUPANCY_KEY)
    return OccupancySensor(
        topic=topic,
        occupancy_key=key if isinstance(key, str) and key else DEFAULT_OCCUPANCY_KEY,
        sweep=sweep,
    )


def _validate_occupancy(
    raw: dict[str, object], source_keys: set[str], issues: list[str]
) -> dict[str, OccupancyZone]:
    """Validate the optional occupancy block; collect soft issues, never fail."""
    raw_occ = raw.get(CONF_OCCUPANCY, {})
    if not isinstance(raw_occ, dict):
        issues.append(f"{CONF_OCCUPANCY}: expected a mapping; ignoring")
        return {}
    zones: dict[str, OccupancyZone] = {}
    for zone_key, zone_raw in raw_occ.items():
        if not isinstance(zone_raw, dict):
            issues.append(f"occupancy.{zone_key}: zone must be a mapping")
            continue
        sensors: dict[str, OccupancySensor] = {}
        raw_sensors = zone_raw.get(CONF_SENSORS, {})
        if isinstance(raw_sensors, dict):
            for name, sensor_raw in raw_sensors.items():
                sensor = _validate_sensor(
                    zone_key, name, sensor_raw, source_keys, issues
                )
                if sensor is not None:
                    sensors[name] = sensor
        if not sensors:
            issues.append(f"occupancy.{zone_key}: no valid sensors/sweeps")
            continue
        raw_off = zone_raw.get(CONF_OFF_LIGHTS, [])
        off_lights = (
            [t for t in raw_off if isinstance(t, str)]
            if isinstance(raw_off, list)
            else []
        )
        off_trans = zone_raw.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
        zones[zone_key] = OccupancyZone(
            sensors=sensors,
            off_lights=off_lights,
            off_transition_s=float(off_trans)
            if isinstance(off_trans, (int, float)) and not isinstance(off_trans, bool)
            else DEFAULT_OCCUPANCY_TRANSITION_S,
        )
    return zones


def validate_config(raw: object) -> ValidatedConfig:
    """Validate raw seed data into a typed config + switch map + issue list."""
    if not isinstance(raw, dict):
        msg = "config must be a mapping"
        raise ValueError(msg)
    raw_sources = raw.get(CONF_SOURCES)
    if not isinstance(raw_sources, dict) or not raw_sources:
        msg = "config has no 'sources'"
        raise ValueError(msg)

    interval = raw.get(CONF_PUSH_INTERVAL, DEFAULT_PUSH_INTERVAL_S)
    issues: list[str] = []
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        issues.append(
            f"{CONF_PUSH_INTERVAL}: expected a positive int, got {interval!r}; "
            f"using {DEFAULT_PUSH_INTERVAL_S}"
        )
        interval = DEFAULT_PUSH_INTERVAL_S

    switch_map: dict[str, tuple[str, str]] = {}
    sources: dict[str, SourceConfig] = {}
    for source_key, source_raw in raw_sources.items():
        sources[source_key] = _validate_source(
            source_key, source_raw, switch_map, issues
        )

    config: LightManConfig = {
        CONF_PUSH_INTERVAL: interval,
        CONF_SOURCES: sources,
        CONF_OCCUPANCY: _validate_occupancy(raw, set(sources), issues),
    }
    return ValidatedConfig(config=config, switch_map=switch_map, issues=issues)
