"""Typed data models for the Light Man integration.

These keep the push payload, seed config, and hold records strongly typed so
mypy-strict catches shape errors at the seams (coordinator <-> config <-> MQTT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from datetime import datetime


# --- Runtime view (derived by config_loader; consumed by coordinator/push) ---
# These mirror the historical source-centric shape. The loader builds them from
# the stored data model below so the coordinator's addressing is untouched (S1).


class RoomConfig(TypedDict):
    """One addressable light group within a (runtime) source group.

    Keyed in ``SourceConfig.rooms`` by its light-ref ``"<room>.<light_id>"``.
    ``set_topic`` is the per-light group (or single-bulb) ``/set`` topic the push
    targets when this light must be skipped from the consolidated flood.
    ``switches`` are the Inovelli switch base topics (``zigbee2mqtt/<name>``) that
    govern (hold) this light group.
    """

    set_topic: str
    switches: list[str]


class SourceConfig(TypedDict, total=False):
    """One runtime source group (overhead / accent / hallway_up / hallway_down).

    The real-elevation engine ``profile`` (a curve resolved from ``curve_ref``)
    is the sole value source; ``rooms`` are the per-light-group addressable units
    used to exclude held light groups from the flood.
    """

    consolidated_topic: str
    transition_s: float
    profile: SourceProfile
    rooms: dict[str, RoomConfig]


class OccupancyLight(TypedDict):
    """One light a sweep stage turns on (at ``source``'s engine value)."""

    set_topic: str
    source: str


class OccupancyStage(TypedDict, total=False):
    """One step of a directional sweep: an optional lead-in delay + its lights."""

    delay_s: float  # seconds to wait before turning this stage's lights on
    lights: list[OccupancyLight]


class OccupancySensor(TypedDict, total=False):
    """One presence binding: a field on an mmwave topic + the sweep it runs.

    ``topic`` is the Z2M device topic; ``occupancy_key`` is the JSON field
    watched (default ``"occupancy"`` — the device's aggregate; set to e.g.
    ``"mmwave_area1_occupancy"`` to watch a single mmwave detection area). Each sensor
    runs its own ``sweep`` on its occupied edge. Two named sensors may share a
    ``topic`` with different ``occupancy_key``s to monitor several areas of one
    switch as independent triggers (no code change — just another sensor entry).
    """

    topic: str
    occupancy_key: str  # JSON field in the mmwave payload (default "occupancy")
    sweep: list[OccupancyStage]


class OccupancyZone(TypedDict, total=False):
    """A presence zone: its named mmwave sensors and the off targets.

    Each sensor (keyed by an arbitrary name) runs its own ``sweep`` on its
    occupied edge (so the lights come on staggered in the direction of
    approach). The zone is cleared when **all** its sensors report no presence,
    at which point ``off_lights`` turn off.
    """

    sensors: dict[str, OccupancySensor]  # sensor name -> its binding
    off_lights: list[str]  # /set topics turned off on all-clear
    off_transition_s: float
    on_transition_s: float  # sweep fade-in (default DEFAULT_OCCUPANCY_TRANSITION_S)
    sync_switches: list[str]  # switch bases whose state follows the zone
    override_switches: list[str]  # switch bases whose up tap overrides motion
    override_idle_s: float  # an override ends after this long with no presence


class LightManConfig(TypedDict):
    """Top-level seed config (``light_man_config`` Store)."""

    push_interval_s: int
    sources: dict[str, SourceConfig]
    occupancy: dict[str, OccupancyZone]


class XY(TypedDict):
    """A CIE xy chromaticity as Z2M accepts it under the ``color`` key."""

    x: float
    y: float


class RGB(TypedDict):
    """An RGB color as Z2M expects it under the ``color`` key."""

    r: int
    g: int
    b: int


class PushPayload(TypedDict, total=False):
    """A stateless group ``/set`` payload. Never carries ``state`` (latch fix)."""

    brightness: int
    transition: float
    color_temp: int
    color: RGB | XY


# --- Adaptive-target engine -------------------------------------------------


class SleepProfile(TypedDict, total=False):
    """Per-source sleep-overlay target (blended in by the sleep toggle ramp)."""

    br: float
    color_mode: str  # color_temp | rgb
    ct: float
    rgb: list[int] | None
    ramp_in_s: float
    ramp_out_s: float


class DayWindow(TypedDict, total=False):
    """Optional forced sunrise/sunset that gates the edges without remapping noon."""

    enabled: bool
    start: str  # "HH:MM" local
    end: str
    edge_transition_s: float
    wind_down_s: float


class SourceProfile(TypedDict, total=False):
    """Per-source adaptive profile (Phase 2 OptionsFlow; replaces the AL switch).

    Endpoints carry over from the current AL config; ``base_color_mode``/``base_rgb``
    add an explicit daytime single-color option (e.g. hallway sky-blue) that AL
    cannot express. See docs/reference/adaptive-algorithm.md.
    """

    min_br: float
    max_br: float
    min_ct: float  # Kelvin — warm endpoint (low elevation)
    max_ct: float  # Kelvin — cool endpoint (high elevation)
    sat: float
    base_color_mode: str  # color_temp | rgb
    base_rgb: list[int] | None
    dusk_floor_ct: float
    night_floor_br: float
    sleep: SleepProfile
    day_window: DayWindow


# --- Stored data model (the dashboard schema; written to the Store / seed) ----
# This is the authority the panel reads/writes; config_loader derives the runtime
# view above from it. See docs/reference/data-model.md.


class SourceGroup(TypedDict, total=False):
    """A curve-bearing consolidated unit (the stored "source").

    ``consolidated_topic`` is the flood target; ``curve_ref`` names a curve in the
    top-level ``curves`` library; ``transition_s`` is an optional per-group push
    transition. Its member light groups are *derived* — every room light whose
    ``source`` names this group.
    """

    consolidated_topic: str
    curve_ref: str
    transition_s: float


class RoomLight(TypedDict, total=False):
    """One fixture mounted in a room: its id, ``/set`` topic, and source group.

    The fixture's house-wide ref is ``"<room_id>.<id>"``; its curve is the
    ``source`` group's ``curve_ref``.
    """

    id: str
    set_topic: str
    source: str


class BarAnimation(TypedDict):
    """A timed whole-bar animation on a switch's LED bar (Z2M ``led_effect``)."""

    effect: str  # e.g. "slow_rising", "pulse"
    color: int  # Inovelli hue wheel, 0-255
    level: int  # 1-100, scaled down with the room's curve
    duration: int  # seconds (1-60); the switch's own longer codes up to 255


class RoomSwitch(TypedDict, total=False):
    """An Inovelli switch in a room and the lights it governs (holds).

    ``governs`` is a light ref ``"<room_id>.<light_id>"`` — the group the switch is
    SBM-bound to and whose hold its taps drive — a list of light refs (a switch
    bound to several lights of a room), or a room id, for a switch bound to every
    light of the room (the hallway pair). ``bar_follows`` is the governed light
    whose curve and level the switch's LED bar shows (default: the first).
    ``animations`` maps a tap (``up_double`` ...) to the bar animation it plays —
    the shade taps.
    """

    topic: str
    governs: str | list[str]
    bar_follows: str
    animations: dict[str, BarAnimation]


class RoomSensor(TypedDict, total=False):
    """An mmwave presence sensor mounted in a room (defined once, here).

    ``occupancy_key`` is the JSON field watched on the device (default
    ``"occupancy"``). Referenced by zones as ``"<room_id>.<sensor_name>"``.
    """

    topic: str
    occupancy_key: str


class Room(TypedDict, total=False):
    """A first-class physical space: its lights, switches, and sensors.

    Merged across source groups (e.g. kitchen overhead + island), so one physical
    room owns several light groups.
    """

    name: str
    lights: list[RoomLight]
    switches: list[RoomSwitch]
    sensors: dict[str, RoomSensor]


class ZoneStage(TypedDict, total=False):
    """One sweep stage: an optional lead-in delay + room-fixture refs to light.

    Each entry of ``lights`` is a fixture ref ``"<room_id>.<light_id>"`` (or a
    switch ref) resolved against the rooms at load time.
    """

    delay_s: float
    lights: list[str]


class ZoneSensorBinding(TypedDict, total=False):
    """A zone's use of one room sensor: the sweep it runs on the occupied edge."""

    sweep: list[ZoneStage]


class ZoneConfig(TypedDict, total=False):
    """A logical, cross-room presence zone referencing room-owned sensors.

    ``sensors`` is keyed by sensor ref ``"<room_id>.<sensor_name>"``; the zone
    clears (turns off ``off_lights``) when all its sensors report no presence.
    """

    sensors: dict[str, ZoneSensorBinding]
    off_lights: list[str]
    off_transition_s: float
    on_transition_s: float
    sync_switches: list[str]
    override_switches: list[str]
    override_idle_s: float


class SleepConfig(TypedDict, total=False):
    """Global sleep-overlay ramp timing (on/off is the runtime sleep switch)."""

    ramp_in_s: float
    ramp_out_s: float


class StoredConfig(TypedDict, total=False):
    """Top-level stored topology (the dashboard data model; ``light_man_config``)."""

    seed_version: int
    push_interval_s: int
    curves: dict[str, SourceProfile]
    sources: dict[str, SourceGroup]
    rooms: dict[str, Room]
    occupancy_zones: dict[str, ZoneConfig]
    sleep: SleepConfig


@dataclass(slots=True)
class EngineTarget:
    """The engine's computed target for one source this cycle.

    Carries its own color mode so the push emits ``color_temp`` or ``color``
    from the engine's decision rather than a static per-source flag.
    """

    brightness_pct: float
    color_mode: str  # const.COLOR_MODE_COLOR_TEMP | COLOR_MODE_RGB | COLOR_MODE_XY
    color_temp_kelvin: float | None
    rgb_color: tuple[int, int, int] | None
    xy: tuple[float, float] | None = None  # set when color_mode is xy


@dataclass(slots=True)
class HeldRecord:
    """A room held at a look: which kind, when set, and when it auto-expires."""

    room: str
    kind: str  # one of const.HELD_KINDS
    armed_at: datetime
    expires_at: datetime
    look: dict[str, Any] | None = None  # manual holds: the level read from the bulb
