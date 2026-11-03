"""Pure push logic: payload build, value conversions, and addressing.

Everything here is hass-free and side-effect-free so it can be unit-tested
directly. The coordinator supplies live values and performs the MQTT publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .const import (
    BRIGHTNESS_MAX,
    COLOR_MODE_RGB,
    COLOR_MODE_XY,
    MIRED_MAX,
    MIRED_MIN,
)

if TYPE_CHECKING:
    from .models import RGB, PushPayload, SourceConfig


def pct_to_brightness(pct: float) -> int:
    """Convert an AL ``brightness_pct`` (0-100) to a Z2M ``brightness`` (0-254)."""
    return max(0, min(BRIGHTNESS_MAX, round(pct / 100 * BRIGHTNESS_MAX)))


def kelvin_to_mired(kelvin: float) -> int:
    """Convert color temperature in Kelvin to mireds, clamped to device range."""
    if kelvin <= 0:
        return MIRED_MAX
    return max(MIRED_MIN, min(MIRED_MAX, round(1_000_000 / kelvin)))


def mired_to_kelvin(mired: float) -> int:
    """Convert mireds back to Kelvin (inverse of :func:`kelvin_to_mired`)."""
    if mired <= 0:
        return 0
    return round(1_000_000 / mired)


def valid_rgb(rgb: object) -> bool:
    """Return True if ``rgb`` is a 3-element sequence of 0-255 integers."""
    if not isinstance(rgb, (list, tuple)) or len(rgb) != 3:
        return False
    return all(isinstance(c, int) and 0 <= c <= 255 for c in rgb)


def build_payload(
    *,
    brightness_pct: float,
    color_temp_kelvin: float,
    rgb_color: object,
    mode: str,
    transition: float,
    xy: tuple[float, float] | None = None,
) -> PushPayload:
    """Build a stateless group ``/set`` payload.

    Emits ``color`` as xy when ``mode`` is xy, as rgb when ``mode`` is rgb *and*
    ``rgb_color`` is valid; otherwise falls back to ``color_temp`` so a missing
    colour never breaks the push.
    """
    payload: PushPayload = {
        "brightness": pct_to_brightness(brightness_pct),
        "transition": transition,
    }
    if mode == COLOR_MODE_XY and xy is not None:
        payload["color"] = {"x": round(xy[0], 4), "y": round(xy[1], 4)}
    elif mode == COLOR_MODE_RGB and valid_rgb(rgb_color):
        seq = cast("tuple[int, int, int]", rgb_color)  # valid_rgb checked the shape
        color: RGB = {"r": int(seq[0]), "g": int(seq[1]), "b": int(seq[2])}
        payload["color"] = color
    else:
        payload["color_temp"] = kelvin_to_mired(color_temp_kelvin)
    return payload


@dataclass(frozen=True)
class PlannedSend:
    """One send in a source's unit; ``dedup`` False for held-room re-asserts."""

    topic: str
    payload: PushPayload
    dedup: bool = True


def plan_source(
    source: SourceConfig,
    *,
    adaptive_payload: PushPayload,
    looks: dict[str, PushPayload | None],
    max_unit: int | None = None,
) -> tuple[str, list[PlannedSend]]:
    """Plan one source's unit for this tick: ``(addressing, sends)``.

    ``looks`` maps each held room to the look it must show, or ``None`` for a
    manual hold whose level is not known yet.

    - No holds (or no rooms): the consolidated topic alone.
    - Holds with known looks: the consolidated flood **then** a re-assert of each
      held room's look, sent as one unit (verified live 2026-11-03: a lit held
      bulb moves at most 1 point / 1 mired between the two frames).
    - Any manual hold with an unknown level, or more re-asserts than fit one
      unit (``1 + held > max_unit``): per-room for this source — every unheld
      room gets its own target, a held room its look (deduped, so a constant
      look costs nothing), and an unknown room is left untouched.
    """
    consolidated = source["consolidated_topic"]
    rooms = source.get("rooms", {})
    held = {room: look for room, look in looks.items() if room in rooms}
    if not held:
        return "consolidated", [PlannedSend(consolidated, adaptive_payload)]
    fits = max_unit is None or 1 + len(held) <= max_unit
    if fits and all(look is not None for look in held.values()):
        reasserts = [
            PlannedSend(
                rooms[room]["set_topic"], cast("PushPayload", look), dedup=False
            )
            for room, look in held.items()
            if rooms[room].get("set_topic")
        ]
        return "reassert", [PlannedSend(consolidated, adaptive_payload), *reasserts]
    sends = []
    for room, room_cfg in rooms.items():
        topic = room_cfg.get("set_topic")
        if not topic:
            continue
        if room not in held:
            sends.append(PlannedSend(topic, adaptive_payload))
        elif held[room] is not None:
            sends.append(PlannedSend(topic, cast("PushPayload", held[room])))
    return "per_room", sends
