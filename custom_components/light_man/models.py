"""Typed data models for the Light Man integration.

These keep the push payload, seed config, and hold records strongly typed so
mypy-strict catches shape errors at the seams (coordinator <-> config <-> MQTT).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from datetime import datetime


class RoomConfig(TypedDict):
    """One addressable room within a source.

    ``set_topic`` is the per-room group (or single-bulb) ``/set`` topic the push
    targets when the room must be skipped from the consolidated flood.
    ``switches`` are the Inovelli switch base topics (``zigbee2mqtt/<name>``)
    whose ``action``/``state`` arm and release the room's hold.
    """

    set_topic: str
    switches: list[str]


class SourceConfig(TypedDict, total=False):
    """One adaptive source (overhead / accent / hallway_up / hallway_down).

    The real-elevation engine ``profile`` is the sole value source; ``rooms`` are
    the per-room addressable groups used to exclude held rooms from the flood.
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


class LightManConfig(TypedDict):
    """Top-level seed config (``light_man_config`` Store)."""

    push_interval_s: int
    sources: dict[str, SourceConfig]
    occupancy: dict[str, OccupancyZone]


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
    color: RGB


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


@dataclass(slots=True)
class EngineTarget:
    """The engine's computed target for one source this cycle.

    Carries its own color mode so the push emits ``color_temp`` or ``color``
    from the engine's decision rather than a static per-source flag.
    """

    brightness_pct: float
    color_mode: str  # const.COLOR_MODE_COLOR_TEMP | COLOR_MODE_RGB
    color_temp_kelvin: float | None
    rgb_color: tuple[int, int, int] | None


@dataclass(slots=True)
class HeldRecord:
    """A room held at a look: which kind, when set, and when it auto-expires."""

    room: str
    kind: str  # one of const.HELD_KINDS
    armed_at: datetime
    expires_at: datetime
