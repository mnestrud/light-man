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
    rooms: dict[str, RoomConfig]


class LightManConfig(TypedDict):
    """Top-level seed config (``light_man_config`` Store)."""

    push_interval_s: int
    sources: dict[str, SourceConfig]


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


@dataclass(slots=True)
class HeldRecord:
    """A room held at a look: which kind, when set, and when it auto-expires."""

    room: str
    kind: str  # one of const.HELD_KINDS
    armed_at: datetime
    expires_at: datetime
