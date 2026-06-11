"""Pure push logic: payload build, value conversions, and addressing.

Everything here is hass-free and side-effect-free so it can be unit-tested
directly. The coordinator supplies live values and performs the MQTT publish.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .const import (
    BRIGHTNESS_MAX,
    COLOR_MODE_RGB,
    HELD_DAY,
    HELD_MANUAL,
    HELD_NIGHT,
    MIRED_MAX,
    MIRED_MIN,
    MODE_ADAPTIVE,
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


def _room_target(
    mode: str,
    *,
    adaptive_payload: PushPayload,
    day_payload: PushPayload,
    night_payload: PushPayload,
) -> PushPayload | None:
    """Return a room's payload, or None to leave it untouched (manually frozen).

    Off rooms are still sent the value: ``hue_native_control`` stages it
    color-while-off so the bulbs adapt while off and turn on uniform.
    """
    if mode == HELD_MANUAL:
        return None
    if mode == HELD_NIGHT:
        return night_payload
    if mode == HELD_DAY:
        return day_payload  # a distinct forced day look, not the live value
    return adaptive_payload


def plan_publishes(
    source: SourceConfig,
    *,
    adaptive_payload: PushPayload,
    day_payload: PushPayload,
    night_payload: PushPayload,
    modes: dict[str, str],
) -> list[tuple[str, PushPayload]]:
    """Plan ``(topic, payload)`` publishes for a source this cycle.

    Use the **consolidated** flood when every room wants the live adaptive value
    (the cheap, uniform case — including off rooms, which stage color-while-off).
    Otherwise address **per-room**: held rooms get their own target (day/night
    look) and manually-frozen rooms are skipped. Sources with no rooms (hallway)
    always push their single consolidated group.
    """
    rooms = source.get("rooms", {})
    if not rooms:
        return [(source["consolidated_topic"], adaptive_payload)]
    targets = {
        room: _room_target(
            modes.get(room, MODE_ADAPTIVE),
            adaptive_payload=adaptive_payload,
            day_payload=day_payload,
            night_payload=night_payload,
        )
        for room in rooms
    }
    if all(target == adaptive_payload for target in targets.values()):
        return [(source["consolidated_topic"], adaptive_payload)]
    return [
        (rooms[room]["set_topic"], target)
        for room, target in targets.items()
        if target is not None
    ]
