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
    CONF_ANIMATIONS,
    CONF_BAR_FOLLOWS,
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
    CONF_ON_TRANSITION,
    CONF_OVERRIDE_IDLE,
    CONF_OVERRIDE_SWITCHES,
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
    CONF_STAGE_GROUP,
    CONF_STEP_MAX_MIRED,
    CONF_STEP_MAX_PCT,
    CONF_STEP_MAX_XY,
    CONF_STEP_MIN,
    CONF_STEPS,
    CONF_SWEEP,
    CONF_SWITCHES,
    CONF_SYNC_SWITCHES,
    CONF_TOPIC,
    CONF_TRANSITION,
    DEFAULT_OCCUPANCY_KEY,
    DEFAULT_OCCUPANCY_TRANSITION_S,
    DEFAULT_OVERRIDE_IDLE_S,
    DEFAULT_PUSH_INTERVAL_S,
    DEFAULT_STEP_MAX_MIRED,
    DEFAULT_STEP_MAX_PCT,
    DEFAULT_STEP_MAX_XY,
    DEFAULT_STEP_MIN_S,
)
from .models import (
    BarAnimation,
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
    StepConfig,
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
    # room-level switches: base -> (room id, [(source, light_ref), ...])
    room_switch_map: dict[str, tuple[str, list[tuple[str, str]]]] = field(
        default_factory=dict
    )
    # switch base -> tap action -> the bar animation it plays (shade taps)
    switch_animations: dict[str, dict[str, BarAnimation]] = field(default_factory=dict)
    # light-level switches: base -> every (source, light_ref) it governs
    switch_lights: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # every switch: base -> the (source, light_ref) its LED bar follows
    bar_lights: dict[str, tuple[str, str]] = field(default_factory=dict)


@dataclass(slots=True)
class _SwitchEntry:
    """A switch as listed in a room: what it governs and what its bar follows."""

    base: str
    governs: str | list[str]
    bar_follows: str | None


@dataclass(slots=True)
class _RoomScan:
    """Indices accumulated from a single pass over the stored ``rooms``."""

    rooms: dict[str, Room] = field(default_factory=dict)
    # light ref "<room>.<id>" -> {"set_topic": ..., "source": <group>}
    light_index: dict[str, dict[str, str]] = field(default_factory=dict)
    # sensor ref "<room>.<name>" -> {topic, occupancy_key}
    sensor_index: dict[str, RoomSensor] = field(default_factory=dict)
    switch_entries: list[_SwitchEntry] = field(default_factory=list)
    switch_topics: set[str] = field(default_factory=set)
    switch_animations: dict[str, dict[str, BarAnimation]] = field(default_factory=dict)
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


def _int_in(value: object, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _switch_animations(
    topic: str, raw: object, issues: list[str]
) -> dict[str, BarAnimation]:
    """Return a switch's tap -> bar animation map, dropping malformed entries."""
    animations: dict[str, BarAnimation] = {}
    if raw is None:
        return animations
    if not isinstance(raw, dict):
        issues.append(f"switch {topic!r}: 'animations' must be a mapping; ignoring")
        return animations
    for action, anim in raw.items():
        if not (
            isinstance(anim, dict)
            and isinstance(anim.get("effect"), str)
            and _int_in(anim.get("color"), 0, 255)
            and _int_in(anim.get("level"), 1, 100)
            and _int_in(anim.get("duration"), 1, 255)
        ):
            issues.append(
                f"switch {topic!r}: animation {action!r} needs effect, "
                "color 0-255, level 1-100, duration 1-255"
            )
            continue
        animations[str(action)] = BarAnimation(
            effect=anim["effect"],
            color=anim["color"],
            level=anim["level"],
            duration=anim["duration"],
        )
    return animations


def _switch_governs(
    topic: str, governs: object, issues: list[str]
) -> str | list[str] | None:
    """Return a switch's ``governs`` (a ref or a list of refs), or None + issue."""
    if isinstance(governs, list) and governs:
        if all(isinstance(ref, str) and ref for ref in governs):
            return [str(ref) for ref in governs]
        issues.append(f"switch {topic!r}: 'governs' list must hold light refs")
        return None
    if isinstance(governs, str) and governs:
        return governs
    issues.append(f"switch {topic!r}: missing 'governs'")
    return None


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
        governs = _switch_governs(topic, governs, issues)
        if governs is None:
            continue
        follows = entry.get(CONF_BAR_FOLLOWS)
        if follows is not None and not isinstance(follows, str):
            issues.append(f"switch {topic!r}: 'bar_follows' must be a light ref")
            follows = None
        scan.switch_entries.append(_SwitchEntry(topic, governs, follows or None))
        scan.switch_topics.add(topic)
        switch = RoomSwitch(topic=topic, governs=governs)
        if follows:
            switch[CONF_BAR_FOLLOWS] = follows
        animations = _switch_animations(topic, entry.get(CONF_ANIMATIONS), issues)
        if animations:
            switch[CONF_ANIMATIONS] = animations
            scan.switch_animations[topic] = animations
        switches.append(switch)
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


def _governed_lights(
    entry: _SwitchEntry, scan: _RoomScan, issues: list[str]
) -> tuple[str | None, list[tuple[str, str]]] | None:
    """Resolve what a switch governs: ``(room id or None, [(source, ref)])``.

    A room id covers every light of that room; a light ref or a list of light
    refs covers those lights. None (with an issue) when it does not resolve.
    """
    base, governs = entry.base, entry.governs
    if isinstance(governs, str) and governs not in scan.light_index:
        if governs not in scan.rooms:
            issues.append(f"switch {base!r}: governs unknown light {governs!r}")
            return None
        prefix = f"{governs}."
        lights = [
            (light[CONF_SOURCE], ref)
            for ref, light in scan.light_index.items()
            if ref.startswith(prefix)
        ]
        if not lights:
            issues.append(f"switch {base!r}: room {governs!r} has no lights")
            return None
        return governs, lights
    refs = [governs] if isinstance(governs, str) else governs
    unknown = [ref for ref in refs if ref not in scan.light_index]
    if unknown:
        issues.append(f"switch {base!r}: governs unknown light {unknown[0]!r}")
        return None
    if len(set(refs)) != len(refs):
        issues.append(f"switch {base!r}: governs a light twice")
        return None
    return None, [(scan.light_index[ref][CONF_SOURCE], ref) for ref in refs]


@dataclass(slots=True)
class _SwitchMaps:
    """The switch indices handed on to :class:`ValidatedConfig`."""

    switch_map: dict[str, tuple[str, str]] = field(default_factory=dict)
    switch_lights: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    room_switch_map: dict[str, tuple[str, list[tuple[str, str]]]] = field(
        default_factory=dict
    )
    bar_lights: dict[str, tuple[str, str]] = field(default_factory=dict)
    governs_by_light: dict[str, list[str]] = field(default_factory=dict)


def _build_switch_map(scan: _RoomScan, issues: list[str]) -> _SwitchMaps:
    """Resolve switch bases to the lights they govern + reverse governs index.

    A switch governing a whole room (``governs`` is a room id) goes to
    ``room_switch_map`` with every light of that room; any other switch goes to
    ``switch_map`` (its bar light) and ``switch_lights`` (every light it governs).
    """
    validated = _SwitchMaps()
    governs_by_light = validated.governs_by_light
    for entry in scan.switch_entries:
        base = entry.base
        resolved = _governed_lights(entry, scan, issues)
        if resolved is None:
            continue
        if base in validated.bar_lights:
            issues.append(f"switch {base!r} is listed twice")
            continue
        room, lights = resolved
        bar = lights[0]
        if entry.bar_follows is not None:
            match = [light for light in lights if light[1] == entry.bar_follows]
            if match:
                bar = match[0]
            else:
                issues.append(
                    f"switch {base!r}: bar_follows {entry.bar_follows!r} "
                    "is not a light it governs"
                )
        validated.bar_lights[base] = bar
        if room is not None:
            validated.room_switch_map[base] = (room, lights)
            continue
        validated.switch_map[base] = bar
        validated.switch_lights[base] = lights
        for _source, ref in lights:
            governs_by_light.setdefault(ref, []).append(base)
    # A light a switch governs must be addressable around (else a hold hits it).
    for ref in scan.empty_topic_refs:
        if ref in governs_by_light:
            issues.append(f"light {ref}: holdable light has no set_topic")
        else:
            issues.append(f"light {ref}: light has no set_topic")
    return validated


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
        group = raw_stage.get(CONF_STAGE_GROUP)
        if isinstance(group, str) and group:
            run_stage[CONF_STAGE_GROUP] = group
            ref_stage[CONF_STAGE_GROUP] = group
        elif group is not None:
            issues.append(f"occupancy.{zone_id}: sweep group {group!r} is not a topic")
        runtime.append(run_stage)
        stored.append(ref_stage)
    return runtime, stored


def _zone_switches(
    zone_id: str,
    zone_raw: dict[str, object],
    key: str,
    scan: _RoomScan,
    issues: list[str],
) -> list[str] | None:
    """Return a zone's switch list (sync or override), or None if absent."""
    if key not in zone_raw:
        return None
    raw_list = zone_raw[key]
    topics = (
        [t for t in raw_list if isinstance(t, str) and t]
        if isinstance(raw_list, list)
        else []
    )
    unknown = [t for t in topics if t not in scan.switch_topics]
    if unknown:
        issues.append(f"occupancy.{zone_id}.{key}: unknown switch {unknown}")
    return [t for t in topics if t in scan.switch_topics]


def _zone_off_transition(zone_raw: dict[str, object]) -> float:
    """Return a zone's all-clear fade-out (the default when unset or bad)."""
    raw = zone_raw.get(CONF_OFF_TRANSITION, DEFAULT_OCCUPANCY_TRANSITION_S)
    if _is_number(raw):
        return float(cast("float", raw))
    return DEFAULT_OCCUPANCY_TRANSITION_S


def _zone_on_transition(
    zone_id: str, zone_raw: dict[str, object], issues: list[str]
) -> float | None:
    """Return a zone's sweep fade-in if set (None: the default applies)."""
    if CONF_ON_TRANSITION not in zone_raw:
        return None
    raw = zone_raw[CONF_ON_TRANSITION]
    if _is_number(raw) and float(cast("float", raw)) >= 0:
        return float(cast("float", raw))
    issues.append(f"occupancy.{zone_id}: bad {CONF_ON_TRANSITION}; default")
    return None


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
    off_transition_s = _zone_off_transition(zone_raw)
    runtime = OccupancyZone(
        sensors=run_sensors, off_lights=off_lights, off_transition_s=off_transition_s
    )
    stored = ZoneConfig(
        sensors=stored_sensors, off_lights=off_lights, off_transition_s=off_transition_s
    )
    on_transition = _zone_on_transition(zone_id, zone_raw, issues)
    if on_transition is not None:
        runtime[CONF_ON_TRANSITION] = stored[CONF_ON_TRANSITION] = on_transition
    _apply_zone_switches(zone_id, zone_raw, scan, issues, runtime, stored)
    return runtime, stored


def _apply_zone_switches(
    zone_id: str,
    zone_raw: dict[str, object],
    scan: _RoomScan,
    issues: list[str],
    runtime: OccupancyZone,
    stored: ZoneConfig,
) -> None:
    """Resolve a zone's sync and override switches (and the override idle time)."""
    sync = _zone_switches(zone_id, zone_raw, CONF_SYNC_SWITCHES, scan, issues)
    if sync is not None:
        runtime[CONF_SYNC_SWITCHES] = stored[CONF_SYNC_SWITCHES] = sync
    override = _zone_switches(zone_id, zone_raw, CONF_OVERRIDE_SWITCHES, scan, issues)
    if override is not None:
        runtime[CONF_OVERRIDE_SWITCHES] = stored[CONF_OVERRIDE_SWITCHES] = override
        idle = zone_raw.get(CONF_OVERRIDE_IDLE, DEFAULT_OVERRIDE_IDLE_S)
        if not _is_number(idle) or float(cast("float", idle)) <= 0:
            issues.append(f"occupancy.{zone_id}: bad {CONF_OVERRIDE_IDLE}; default")
            idle = DEFAULT_OVERRIDE_IDLE_S
        seconds = float(cast("float", idle))
        runtime[CONF_OVERRIDE_IDLE] = stored[CONF_OVERRIDE_IDLE] = seconds


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


def _validate_steps(
    raw: dict[str, object], interval: int, issues: list[str]
) -> StepConfig:
    """Validate the variable-step block (soft: each bad value takes its default)."""
    raw_steps = raw.get(CONF_STEPS, {})
    if not isinstance(raw_steps, dict):
        issues.append(f"{CONF_STEPS}: expected a mapping; using defaults")
        raw_steps = {}
    values: dict[str, float] = {}
    for key, default in (
        (CONF_STEP_MIN, DEFAULT_STEP_MIN_S),
        (CONF_STEP_MAX_PCT, DEFAULT_STEP_MAX_PCT),
        (CONF_STEP_MAX_MIRED, DEFAULT_STEP_MAX_MIRED),
        (CONF_STEP_MAX_XY, DEFAULT_STEP_MAX_XY),
    ):
        value = raw_steps.get(key, default)
        if not _is_number(value) or float(cast("float", value)) <= 0:
            issues.append(f"{CONF_STEPS}.{key}: expected a positive number; default")
            value = default
        values[key] = float(cast("float", value))
    if values[CONF_STEP_MIN] > interval:
        issues.append(f"{CONF_STEPS}.{CONF_STEP_MIN}: above {CONF_PUSH_INTERVAL}")
        values[CONF_STEP_MIN] = float(interval)
    return StepConfig(
        min_s=values[CONF_STEP_MIN],
        max_brightness_pct=values[CONF_STEP_MAX_PCT],
        max_mired=values[CONF_STEP_MAX_MIRED],
        max_xy=values[CONF_STEP_MAX_XY],
    )


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
    maps = _build_switch_map(scan, issues)
    runtime_sources = _build_runtime_sources(
        groups, curves, scan, maps.governs_by_light
    )
    runtime_zones, stored_zones = _validate_zones(raw, scan, issues)
    sleep = _validate_sleep(raw)
    steps = _validate_steps(raw, interval, issues)

    config: LightManConfig = {
        CONF_PUSH_INTERVAL: interval,
        CONF_SOURCES: runtime_sources,
        CONF_OCCUPANCY: runtime_zones,
        CONF_STEPS: steps,
    }
    stored: StoredConfig = {
        CONF_PUSH_INTERVAL: interval,
        CONF_CURVES: curves,
        CONF_SOURCES: groups,
        CONF_ROOMS: scan.rooms,
        CONF_OCCUPANCY_ZONES: stored_zones,
        CONF_SLEEP: sleep,
        CONF_STEPS: steps,
    }
    if isinstance(raw.get("seed_version"), int):
        stored["seed_version"] = raw["seed_version"]
    return ValidatedConfig(
        config=config,
        stored=stored,
        switch_map=maps.switch_map,
        issues=issues,
        room_switch_map=maps.room_switch_map,
        switch_animations=scan.switch_animations,
        switch_lights=maps.switch_lights,
        bar_lights=maps.bar_lights,
    )
