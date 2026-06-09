"""Pure push logic: payload build, value conversions, and addressing.

Everything here is hass-free and side-effect-free so it can be unit-tested
directly. The coordinator supplies live values and performs the MQTT publish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .const import (
    BRIGHTNESS_MAX,
    COLOR_MODE_RGB,
    DEFAULT_COLOR_MODE,
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


def resolve_color_mode(source: SourceConfig, *, sleeping: bool) -> str:
    """Pick the active color mode for a source given the sleep state."""
    if sleeping:
        return source.get("night_color_mode", DEFAULT_COLOR_MODE)
    return source.get("day_color_mode", DEFAULT_COLOR_MODE)


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
) -> PushPayload:
    """Build a stateless group ``/set`` payload.

    Emits ``color`` only when ``mode`` is rgb *and* ``rgb_color`` is valid;
    otherwise falls back to ``color_temp`` so a missing RGB never breaks the push.
    """
    payload: PushPayload = {
        "brightness": pct_to_brightness(brightness_pct),
        "transition": transition,
    }
    if mode == COLOR_MODE_RGB and valid_rgb(rgb_color):
        seq = cast("tuple[int, int, int]", rgb_color)  # valid_rgb checked the shape
        color: RGB = {"r": int(seq[0]), "g": int(seq[1]), "b": int(seq[2])}
        payload["color"] = color
    else:
        payload["color_temp"] = kelvin_to_mired(color_temp_kelvin)
    return payload


def select_targets(source: SourceConfig, held_rooms: set[str]) -> list[str]:
    """Choose which topic(s) to flood for a source given the held set.

    No room in this source is held -> the single consolidated groupcast (today's
    RF). Otherwise -> one per-room groupcast for every *unheld* room, skipping
    the held ones (held rooms keep their manual scene).
    """
    rooms = source.get("rooms", {})
    held_here = held_rooms & set(rooms)
    if not held_here:
        return [source["consolidated_topic"]]
    return [cfg["set_topic"] for room, cfg in rooms.items() if room not in held_here]
