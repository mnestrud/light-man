"""Validate the stored topology data model and derive the runtime view.

The **stored** shape (docs/reference/data-model.md) is a top-level curve library,
curve-bearing source *groups* (carrying a ``curve_ref``), first-class rooms that
own ``lights``/``switches``/``sensors``, cross-room ``occupancy_zones`` that
reference room sensors, and a ``sleep`` block.

Structural problems (not a mapping; no sources; a group missing its consolidated
topic or curve_ref; an unknown curve) raise :class:`ValueError` so
``_async_setup`` can turn them into ``ConfigEntryNotReady``. Softer problems (a
holdable light with no ``set_topic``, a switch governing an unknown light, an
occupancy ref to a missing room/sensor) are collected as issues and surfaced in
diagnostics — they never fail setup.

The coordinator and push logic consume the **derived runtime view** (source groups
whose ``rooms`` are keyed by light-ref ``"<room>.<id>"``), which is byte-identical
in addressing to the historical source-centric model (data-model.md S1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .const import (
    CONF_CONSOLIDATED_TOPIC,
    CONF_CURVE_REF,
    CONF_CURVES,
    CONF_GOVERNS,
    CONF_ID,
    CONF_LIGHTS,
    CONF_NAME,
    CONF_OCCUPANCY,
    CONF_OCCUPANCY_KEY,
    CONF_OCCUPANCY_ZONES,
    CONF_OFF_LIGHTS,
    CONF_OFF_TRANSITION,
    CONF_PROFILE,
    CONF_PUSH_INTERVAL,
    CONF_RAMP_IN,
    CONF_RAMP_OUT,
    CONF_ROOMS,
    CONF_SENSORS,
    CONF_SET_TOPIC,
    CONF_SLEEP,
    CONF_SOURCE,
    CONF_SOURCES,
    CONF_STAGE_DELAY,
    CONF_SWEEP,
    CONF_SWITCHES,
    CONF_TOPIC,
    CONF_TRANSITION,
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
    Room,
    RoomConfig,
    RoomLight,
    RoomSensor,
    RoomSwitch,
    SleepConfig,
    SourceConfig,
    SourceGroup,
    SourceProfile,
    StoredConfig,
    ZoneConfig,
    ZoneSensorBinding,
    ZoneStage,
)


@dataclass(slots=True)
class ValidatedConfig:
    """Result of validating the seed config.

    ``config`` is the derived **runtime** view (consumed by the coordinator and
    push logic); ``stored`` is the **data-model** shape (the authority the panel
    reads/writes); ``switch_map`` resolves an Inovelli switch base topic to its
    ``(source_group, light_ref)`` — the group it re-pushes and the light group it
    holds.
    """

    config: LightManConfig
    stored: StoredConfig
    switch_map: dict[str, tuple[str, str]]
    issues: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _RoomScan:
    """Indices accumulated from a single pass over the stored ``rooms``."""

    rooms: dict[str, Room] = field(default_factory=dict)
    # light ref "<room>.<id>" -> {"set_topic": ..., "source": <group>}
    light_index: dict[str, dict[str, str]] = field(default_factory=dict)
    # sensor ref "<room>.<name>" -> {topic, occupancy_key}
    sensor_index: dict[str, RoomSensor] = field(default_factory=dict)
    # (switch base topic, governed light ref)
    switch_entries: list[tuple[str, str]] = field(default_factory=list)
    # light refs whose set_topic was missing/empty (flagged after switch scan)
    empty_topic_refs: set[str] = field(default_factory=set)


def _is_number(value: object) -> bool:
    """Return True for a real int/float (not a bool, an int subclass)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --- curves + source groups -------------------------------------------------


def _validate_curves(
    raw: dict[str, object], issues: list[str]
) -> dict[str, SourceProfile]:
    """Validate the top-level curve library; drop non-mapping entries softly."""
    raw_curves = raw.get(CONF_CURVES, {})
    if not isinstance(raw_curves, dict):
        msg = f"'{CONF_CURVES}' must be a mapping"
        raise ValueError(msg)
    curves: dict[str, SourceProfile] = {}
    for name, curve in raw_curves.items():
        if not isinstance(curve, dict):
            issues.append(f"curve {name!r}: must be a mapping; ignoring")
            continue
        curves[name] = cast("SourceProfile", curve)
    return curves


def _validate_group(
    key: str, raw: object, curves: dict[str, SourceProfile]
) -> SourceGroup:
    """Validate one source group; structural errors raise.

    A group needs a ``consolidated_topic`` (flood target) and a ``curve_ref`` that
    names a library curve. ``transition_s`` is the only optional carry-through.
    """
    if not isinstance(raw, dict):
        msg = f"source {key!r} must be a mapping"
        raise ValueError(msg)
    consolidated = raw.get(CONF_CONSOLIDATED_TOPIC)
    curve_ref = raw.get(CONF_CURVE_REF)
    if not isinstance(consolidated, str) or not consolidated:
        msg = f"source {key!r} is missing '{CONF_CONSOLIDATED_TOPIC}'"
        raise ValueError(msg)
    if not isinstance(curve_ref, str) or not curve_ref:
        msg = f"source {key!r} is missing '{CONF_CURVE_REF}'"
        raise ValueError(msg)
    if curve_ref not in curves:
        msg = f"source {key!r} references unknown curve {curve_ref!r}"
        raise ValueError(msg)
    group: SourceGroup = {
        CONF_CONSOLIDATED_TOPIC: consolidated,
        CONF_CURVE_REF: curve_ref,
    }
    if CONF_TRANSITION in raw and _is_number(raw[CONF_TRANSITION]):
        group[CONF_TRANSITION] = float(cast("float", raw[CONF_TRANSITION]))
    return group


def _validate_groups(
    raw: dict[str, object], curves: dict[str, SourceProfile]
) -> dict[str, SourceGroup]:
    """Validate every source group (structural)."""
    raw_sources = raw.get(CONF_SOURCES)
    if not isinstance(raw_sources, dict) or not raw_sources:
        msg = "config has no 'sources'"
        raise ValueError(msg)
    return {
        key: _validate_group(key, value, curves) for key, value in raw_sources.items()
    }


# --- rooms (lights / switches / sensors) ------------------------------------


def _scan_room_lights(
    room_id: str,
    room_raw: dict[str, object],
    group_keys: set[str],
    scan: _RoomScan,
    issues: list[str],
) -> list[RoomLight]:
    """Index a room's lights into ``scan`` and return the normalized list."""
    lights_raw = room_raw.get(CONF_LIGHTS, [])
    lights: list[RoomLight] = []
    if not isinstance(lights_raw, list):
        issues.append(f"room {room_id!r}: 'lights' must be a list; ignoring")
        return lights
    for entry in lights_raw:
        if not isinstance(entry, dict):
            issues.append(f"room {room_id!r}: light must be a mapping")
            continue
        light_id = entry.get(CONF_ID)
        source = entry.get(CONF_SOURCE)
        set_topic = entry.get(CONF_SET_TOPIC)
        if not isinstance(light_id, str) or not light_id:
            issues.append(f"room {room_id!r}: light missing id")
            continue
        ref = f"{room_id}.{light_id}"
        if not isinstance(source, str) or source not in group_keys:
            issues.append(f"light {ref}: unknown source {source!r}")
            continue
        if not isinstance(set_topic, str) or not set_topic:
            set_topic = ""
            scan.empty_topic_refs.add(ref)
        scan.light_index[ref] = {CONF_SET_TOPIC: set_topic, CONF_SOURCE: source}
        lights.append(RoomLight(id=light_id, set_topic=set_topic, source=source))
    return lights


def _scan_room_switches(
    room_id: str, room_raw: dict[str, object], scan: _RoomScan, issues: list[str]
) -> list[RoomSwitch]:
    """Index a room's switches into ``scan`` and return the normalized list."""
    switches_raw = room_raw.get(CONF_SWITCHES, [])
    switches: list[RoomSwitch] = []
    if not isinstance(switches_raw, list):
        issues.append(f"room {room_id!r}: 'switches' must be a list; ignoring")
        return switches
    for entry in switches_raw:
        if not isinstance(entry, dict):
            issues.append(f"room {room_id!r}: switch must be a mapping")
            continue
        topic = entry.get(CONF_TOPIC)
        governs = entry.get(CONF_GOVERNS)
        if not isinstance(topic, str) or not topic:
            issues.append(f"room {room_id!r}: switch missing topic")
            continue
        if not isinstance(governs, str) or not governs:
            issues.append(f"switch {topic!r}: missing 'governs'")
            continue
        scan.switch_entries.append((topic, governs))
        switches.append(RoomSwitch(topic=topic, governs=governs))
    return switches


def _scan_room_sensors(
    room_id: str, room_raw: dict[str, object], scan: _RoomScan, issues: list[str]
) -> dict[str, RoomSensor]:
    """Index a room's sensors into ``scan`` and return the normalized map."""
    sensors_raw = room_raw.get(CONF_SENSORS, {})
    sensors: dict[str, RoomSensor] = {}
    if not isinstance(sensors_raw, dict):
        issues.append(f"room {room_id!r}: 'sensors' must be a mapping; ignoring")
        return sensors
    for name, entry in sensors_raw.items():
        if not isinstance(entry, dict):
            issues.append(f"room {room_id!r}.{name}: sensor must be a mapping")
            continue
        topic = entry.get(CONF_TOPIC)
        if not isinstance(topic, str) or not topic:
            issues.append(f"room {room_id!r}.{name}: sensor missing topic")
            continue
        key = entry.get(CONF_OCCUPANCY_KEY, DEFAULT_OCCUPANCY_KEY)
        sensor = RoomSensor(
            topic=topic,
            occupancy_key=key
            if isinstance(key, str) and key
            else DEFAULT_OCCUPANCY_KEY,
        )
        sensors[name] = sensor
        scan.sensor_index[f"{room_id}.{name}"] = sensor
    return sensors


def _scan_rooms(
    raw: dict[str, object], group_keys: set[str], issues: list[str]
) -> _RoomScan:
    """Validate every room, building the light/sensor/switch indices."""
    raw_rooms = raw.get(CONF_ROOMS, {})
    if not isinstance(raw_rooms, dict):
        msg = f"'{CONF_ROOMS}' must be a mapping"
        raise ValueError(msg)
    scan = _RoomScan()
    for room_id, room_raw in raw_rooms.items():
        if not isinstance(room_raw, dict):
            msg = f"room {room_id!r} must be a mapping"
            raise ValueError(msg)
        name = room_raw.get(CONF_NAME)
        room: Room = {
            CONF_NAME: name if isinstance(name, str) and name else room_id,
            CONF_LIGHTS: _scan_room_lights(room_id, room_raw, group_keys, scan, issues),
            CONF_SWITCHES: _scan_room_switches(room_id, room_raw, scan, issues),
            CONF_SENSORS: _scan_room_sensors(room_id, room_raw, scan, issues),
        }
        scan.rooms[room_id] = room
    return scan


def _build_switch_map(
    scan: _RoomScan, issues: list[str]
) -> tuple[dict[str, tuple[str, str]], dict[str, list[str]]]:
    """Resolve switch bases to ``(group, light_ref)`` + reverse governs index."""
    switch_map: dict[str, tuple[str, str]] = {}
    governs_by_light: dict[str, list[str]] = {}
    for base, governs in scan.switch_entries:
        info = scan.light_index.get(governs)
        if info is None:
            issues.append(f"switch {base!r}: governs unknown light {governs!r}")
            continue
        mapping = (info[CONF_SOURCE], governs)
        if base in switch_map:
            issues.append(
                f"switch {base!r} maps to both {switch_map[base]} and {mapping}"
            )
            continue
        switch_map[base] = mapping
        governs_by_light.setdefault(governs, []).append(base)
    # A light a switch governs must be addressable around (else a hold hits it).
    for ref in scan.empty_topic_refs:
        if ref in governs_by_light:
            issues.append(f"light {ref}: holdable light has no set_topic")
        else:
            issues.append(f"light {ref}: light has no set_topic")
    return switch_map, governs_by_light


def _build_runtime_sources(
    groups: dict[str, SourceGroup],
    curves: dict[str, SourceProfile],
    scan: _RoomScan,
    governs_by_light: dict[str, list[str]],
) -> dict[str, SourceConfig]:
    """Derive the runtime source groups: each carries its curve + member lights."""
    members: dict[str, dict[str, RoomConfig]] = {key: {} for key in groups}
    for ref, info in scan.light_index.items():
        members[info[CONF_SOURCE]][ref] = RoomConfig(
            set_topic=info[CONF_SET_TOPIC],
            switches=governs_by_light.get(ref, []),
        )
    runtime: dict[str, SourceConfig] = {}
    for key, group in groups.items():
        source: SourceConfig = {
            CONF_CONSOLIDATED_TOPIC: group[CONF_CONSOLIDATED_TOPIC],
            CONF_PROFILE: curves[group[CONF_CURVE_REF]],
            CONF_ROOMS: members[key],
        }
        if CONF_TRANSITION in group:
            source[CONF_TRANSITION] = group[CONF_TRANSITION]
        runtime[key] = source
    return runtime


# --- occupancy zones --------------------------------------------------------


def _resolve_sweep(
    zone_id: str,
    raw_sweep: object,
    light_index: dict[str, dict[str, str]],
    issues: list[str],
) -> tuple[list[OccupancyStage], list[ZoneStage]]:
    """Resolve a sensor's sweep: runtime stages (set_topic+source) + stored refs."""
    runtime: list[OccupancyStage] = []
    stored: list[ZoneStage] = []
    if not isinstance(raw_sweep, list):
        return runtime, stored
    for raw_stage in raw_sweep:
        if not isinstance(raw_stage, dict):
            continue
        raw_lights = raw_stage.get(CONF_LIGHTS, [])
        run_lights: list[OccupancyLight] = []
        ref_lights: list[str] = []
        for ref in raw_lights if isinstance(raw_lights, list) else []:
            if not isinstance(ref, str) or ref not in light_index:
                issues.append(
                    f"occupancy.{zone_id}: sweep light {ref!r} owned by no room"
                )
                continue
            info = light_index[ref]
            run_lights.append(
                OccupancyLight(set_topic=info[CONF_SET_TOPIC], source=info[CONF_SOURCE])
            )
            ref_lights.append(ref)
        if not run_lights:
            continue
        run_stage: OccupancyStage = {CONF_LIGHTS: run_lights}
        ref_stage: ZoneStage = {CONF_LIGHTS: ref_lights}
        delay = raw_stage.get(CONF_STAGE_DELAY)
        if _is_number(delay):
            run_stage[CONF_STAGE_DELAY] = float(cast("float", delay))
            ref_stage[CONF_STAGE_DELAY] = float(cast("float", delay))
        runtime.append(run_stage)
        stored.append(ref_stage)
    return runtime, stored


def _validate_zone(
    zone_id: str,
    zone_raw: dict[str, object],
    scan: _RoomScan,
    issues: list[str],
) -> tuple[OccupancyZone, ZoneConfig] | None:
    """Resolve one zone to its runtime + stored shape; None if it has no sensors."""
    run_sensors: dict[str, OccupancySensor] = {}
    stored_sensors: dict[str, ZoneSensorBinding] = {}
    raw_sensors = zone_raw.get(CONF_SENSORS, {})
    if isinstance(raw_sensors, dict):
        for ref, binding_raw in raw_sensors.items():
            sensor = scan.sensor_index.get(ref)
            if sensor is None:
                issues.append(f"occupancy.{zone_id}: sensor {ref!r} owned by no room")
                continue
            binding = binding_raw if isinstance(binding_raw, dict) else {}
            run_sweep, ref_sweep = _resolve_sweep(
                zone_id, binding.get(CONF_SWEEP, []), scan.light_index, issues
            )
            if not run_sweep:
                issues.append(f"occupancy.{zone_id}.{ref}: no valid sweep")
                continue
            name = ref.rsplit(".", 1)[-1]
            if name in run_sensors:
                issues.append(f"occupancy.{zone_id}: duplicate sensor name {name!r}")
                continue
            run_sensors[name] = OccupancySensor(
                topic=sensor[CONF_TOPIC],
                occupancy_key=sensor.get(CONF_OCCUPANCY_KEY, DEFAULT_OCCUPANCY_KEY),
                sweep=run_sweep,
            )
            stored_sensors[ref] = ZoneSensorBinding(sweep=ref_sweep)
    if not run_sensors:
        issues.append(f"occupancy.{zone_id}: no valid sensors/sweeps")
        return None
    raw_off = zone_raw.get(CONF_OFF_LIGHTS, [])
    off_lights = (
        [t for t in raw_off if isinstance(t, str)] if isinstance(raw_off, list) else []
    )
    off_trans = zone_raw.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
    off_transition_s = (
        float(cast("float", off_trans))
        if _is_number(off_trans)
        else DEFAULT_OCCUPANCY_TRANSITION_S
    )
    runtime = OccupancyZone(
        sensors=run_sensors, off_lights=off_lights, off_transition_s=off_transition_s
    )
    stored = ZoneConfig(
        sensors=stored_sensors, off_lights=off_lights, off_transition_s=off_transition_s
    )
    return runtime, stored


def _validate_zones(
    raw: dict[str, object], scan: _RoomScan, issues: list[str]
) -> tuple[dict[str, OccupancyZone], dict[str, ZoneConfig]]:
    """Validate ``occupancy_zones``; collect soft issues, never fail."""
    raw_zones = raw.get(CONF_OCCUPANCY_ZONES, {})
    if not isinstance(raw_zones, dict):
        issues.append(f"{CONF_OCCUPANCY_ZONES}: expected a mapping; ignoring")
        return {}, {}
    runtime: dict[str, OccupancyZone] = {}
    stored: dict[str, ZoneConfig] = {}
    for zone_id, zone_raw in raw_zones.items():
        if not isinstance(zone_raw, dict):
            issues.append(f"occupancy.{zone_id}: zone must be a mapping")
            continue
        result = _validate_zone(zone_id, zone_raw, scan, issues)
        if result is not None:
            runtime[zone_id], stored[zone_id] = result
    return runtime, stored


# --- sleep + top level ------------------------------------------------------


def _validate_sleep(raw: dict[str, object]) -> SleepConfig:
    """Parse the optional global sleep-ramp block."""
    raw_sleep = raw.get(CONF_SLEEP, {})
    sleep: SleepConfig = {}
    if isinstance(raw_sleep, dict):
        for key in (CONF_RAMP_IN, CONF_RAMP_OUT):
            value = raw_sleep.get(key)
            if _is_number(value):
                sleep[key] = float(cast("float", value))
    return sleep


def _validate_interval(raw: dict[str, object], issues: list[str]) -> int:
    """Validate ``push_interval_s`` (soft — defaults on a bad value)."""
    interval = raw.get(CONF_PUSH_INTERVAL, DEFAULT_PUSH_INTERVAL_S)
    if not isinstance(interval, int) or isinstance(interval, bool) or interval <= 0:
        issues.append(
            f"{CONF_PUSH_INTERVAL}: expected a positive int, got {interval!r}; "
            f"using {DEFAULT_PUSH_INTERVAL_S}"
        )
        return DEFAULT_PUSH_INTERVAL_S
    return interval


def validate_config(raw: object) -> ValidatedConfig:
    """Validate the stored data model into runtime + stored config + switch map."""
    if not isinstance(raw, dict):
        msg = "config must be a mapping"
        raise ValueError(msg)

    issues: list[str] = []
    interval = _validate_interval(raw, issues)
    curves = _validate_curves(raw, issues)
    groups = _validate_groups(raw, curves)
    scan = _scan_rooms(raw, set(groups), issues)
    switch_map, governs_by_light = _build_switch_map(scan, issues)
    runtime_sources = _build_runtime_sources(groups, curves, scan, governs_by_light)
    runtime_zones, stored_zones = _validate_zones(raw, scan, issues)
    sleep = _validate_sleep(raw)

    config: LightManConfig = {
        CONF_PUSH_INTERVAL: interval,
        CONF_SOURCES: runtime_sources,
        CONF_OCCUPANCY: runtime_zones,
    }
    stored: StoredConfig = {
        CONF_PUSH_INTERVAL: interval,
        CONF_CURVES: curves,
        CONF_SOURCES: groups,
        CONF_ROOMS: scan.rooms,
        CONF_OCCUPANCY_ZONES: stored_zones,
        CONF_SLEEP: sleep,
    }
    return ValidatedConfig(
        config=config, stored=stored, switch_map=switch_map, issues=issues
    )
