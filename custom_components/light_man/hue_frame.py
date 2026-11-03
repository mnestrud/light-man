"""Philips Hue manufacturer-specific light frame (cluster 0xFC03) codec.

One format serves both directions: the ``multiColor`` command (0x00) that
Zigbee2MQTT sends when ``hue_native_control`` is on, and the bulb's composite
state attribute (0x0002) that Z2M publishes verbatim as ``philips_raw`` (hex).

Layout: a little-endian ``u16`` flag word, then one field per set flag, in a
fixed wire order that is **not** the flag-bit order. Sources (read
2026-11-02): zigbee-herdsman-converters ``lib/philips.ts``
(``EncodeManuSpecificPhilips2`` / ``DecodeManuSpecificPhilips2``) and Bifrost's
independent reverse-engineering (``doc/hue-zigbee-format.md``).

Pure and hass-free: used by the integration (reading bulbs' own reports) and by
the ``scripts/`` measurement tools.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

FLAG_ON_OFF = 0x0001
FLAG_BRIGHTNESS = 0x0002
FLAG_MIREK = 0x0004
FLAG_XY = 0x0008
FLAG_FADE = 0x0010
FLAG_EFFECT_TYPE = 0x0020
FLAG_GRADIENT_PARAMS = 0x0040
FLAG_EFFECT_SPEED = 0x0080
FLAG_GRADIENT_COLORS = 0x0100
KNOWN_FLAGS = 0x01FF

XY_SCALE = 0xFFFF
BRIGHTNESS_MIN = 1
BRIGHTNESS_MAX = 254


@dataclass(frozen=True)
class HueFrame:
    """Decoded fields; ``None`` means the field's flag was not set."""

    on: bool | None = None
    brightness: int | None = None
    mirek: int | None = None
    xy: tuple[float, float] | None = None
    fade_ds: int | None = None  # deciseconds
    effect_type: int | None = None
    gradient_colors: bytes | None = None  # raw block, size byte excluded
    effect_speed: int | None = None
    gradient_params: tuple[int, int] | None = None

    @property
    def fade_s(self) -> float | None:
        """Fade time in seconds, if present."""
        return None if self.fade_ds is None else self.fade_ds / 10


def _take(data: bytes, pos: int, size: int) -> tuple[bytes, int]:
    end = pos + size
    if end > len(data):
        msg = f"frame truncated at byte {pos} (need {size}, have {len(data) - pos})"
        raise ValueError(msg)
    return data[pos:end], end


def _u8(chunk: bytes) -> int:
    return chunk[0]


def _u16(chunk: bytes) -> int:
    return int(struct.unpack("<H", chunk)[0])


def _xy(chunk: bytes) -> tuple[float, float]:
    x, y = struct.unpack("<HH", chunk)
    return (round(x / XY_SCALE, 4), round(y / XY_SCALE, 4))


# Wire order (not flag-bit order): (flag, field, size, parser). A size of 0 means
# a block prefixed by its own length byte (gradient colours).
_WIRE: tuple[tuple[int, str, int, Callable[[bytes], object]], ...] = (
    (FLAG_ON_OFF, "on", 1, lambda c: c[0] != 0),
    (FLAG_BRIGHTNESS, "brightness", 1, _u8),
    (FLAG_MIREK, "mirek", 2, _u16),
    (FLAG_XY, "xy", 4, _xy),
    (FLAG_FADE, "fade_ds", 2, _u16),
    (FLAG_EFFECT_TYPE, "effect_type", 1, _u8),
    (FLAG_GRADIENT_COLORS, "gradient_colors", 0, bytes),
    (FLAG_EFFECT_SPEED, "effect_speed", 1, _u8),
    (FLAG_GRADIENT_PARAMS, "gradient_params", 2, lambda c: (c[0], c[1])),
)


def decode(data: bytes) -> HueFrame:
    """Decode a frame. Raises ``ValueError`` on unknown flags or a bad length."""
    raw_flags, pos = _take(data, 0, 2)
    flags = _u16(raw_flags)
    if flags & ~KNOWN_FLAGS:
        msg = f"unknown flag bits 0x{flags & ~KNOWN_FLAGS:04x}"
        raise ValueError(msg)
    fields: dict[str, object] = {}
    for flag, name, size, parse in _WIRE:
        if not flags & flag:
            continue
        if size == 0:
            length, pos = _take(data, pos, 1)
            size = length[0]
        chunk, pos = _take(data, pos, size)
        fields[name] = parse(chunk)
    if pos != len(data):
        msg = f"{len(data) - pos} trailing byte(s) after the last flagged field"
        raise ValueError(msg)
    return HueFrame(**fields)  # type: ignore[arg-type]  # keys mirror the fields


def decode_hex(hex_str: str) -> HueFrame:
    """Decode Z2M's ``philips_raw`` hex string."""
    return decode(bytes.fromhex(hex_str))


def encode(frame: HueFrame) -> bytes:
    """Encode the fields Light Man uses (on/off, brightness, mirek, xy, fade).

    Effects and gradients are deliberately unsupported: Light Man never sends
    them, and bits it does not send must stay clear (bulbs may reject frames
    with flags they do not expect).
    """
    if any(
        v is not None
        for v in (
            frame.effect_type,
            frame.gradient_colors,
            frame.effect_speed,
            frame.gradient_params,
        )
    ):
        msg = "effects and gradients are not encodable by Light Man"
        raise ValueError(msg)
    flags = 0
    body = b""
    if frame.on is not None:
        flags |= FLAG_ON_OFF
        body += bytes([1 if frame.on else 0])
    if frame.brightness is not None:
        if not BRIGHTNESS_MIN <= frame.brightness <= BRIGHTNESS_MAX:
            msg = f"brightness {frame.brightness} outside 1..254"
            raise ValueError(msg)
        flags |= FLAG_BRIGHTNESS
        body += bytes([frame.brightness])
    if frame.mirek is not None:
        flags |= FLAG_MIREK
        body += struct.pack("<H", frame.mirek)
    if frame.xy is not None:
        x, y = frame.xy
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            msg = f"xy {frame.xy} outside 0..1"
            raise ValueError(msg)
        flags |= FLAG_XY
        body += struct.pack("<HH", round(x * XY_SCALE), round(y * XY_SCALE))
    if frame.fade_ds is not None:
        flags |= FLAG_FADE
        body += struct.pack("<H", frame.fade_ds)
    return struct.pack("<H", flags) + body
