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
    """One adaptive source (overhead / accent / hallway_up / hallway_down)."""

    al_switch: str
    consolidated_topic: str
    legacy_enable: str | None
    day_color_mode: str
    night_color_mode: str
    sleep_switch: str | None
    transition_s: float
    # Light-Man-owned night-hold target (Phase 2's engine replaces these).
    night_brightness_pct: float
    night_color_temp_kelvin: float
    night_rgb: list[int] | None
    # Phase 2 adaptive-engine profile (seeded from the live AL config). Carried
    # through the loader untouched until the engine wires into the coordinator.
    profile: SourceProfile
    rooms: dict[str, RoomConfig]


class OccupancyLight(TypedDict):
    """One light a zone turns on (at ``source``'s engine value) on presence."""

    set_topic: str
    source: str


class OccupancyZone(TypedDict, total=False):
    """A presence zone: mmwave sensors that gate a set of lights.

    Occupied when **any** ``mmwave_topics`` reports presence; cleared when all
    do not. On the occupied edge the lights turn on at their source's live
    engine value; on the cleared edge they turn off.
    """

    mmwave_topics: list[str]
    occupancy_key: str  # JSON field in the mmwave payload (default "occupancy")
    lights: list[OccupancyLight]
    transition_s: float


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


class AdaptiveValues(TypedDict):
    """The three attributes read from an AL dummy switch."""

    brightness_pct: float
    color_temp_kelvin: float
    rgb_color: list[int] | None


# --- Phase 2: adaptive-target engine ---------------------------------------


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
