"""Drift check: does an off bulb still hold the look Light Man staged on it.

A Hue bulb keeps the colour and brightness it was last sent while it is off,
and Light Man relies on that: a turn-on (switch binding, motion pair group)
carries no colour. A bulb that loses power comes back at its factory look
(full brightness, 2700 K), and nothing re-sends an unchanging look, so it
would come on wrong. Each bulb's own state report (Z2M's ``philips_raw``,
decoded by :mod:`hue_frame`) is compared here with the look it should hold.

Tolerances are wide on purpose: Z2M's rgb→xy and the bulb's gamut clamp move
a colour by up to ~0.045 xy (measured on the hallway uplights, 2026-11-10),
and an off bulb may lag the curve by a step. A factory reset is far outside
them (0.24 xy from the hallway blue; 70 mired from a daytime white).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from .colorspace import kelvin_to_xy, rgb_to_xy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .hue_frame import HueFrame

BRIGHTNESS_TOLERANCE = 15  # raw 0-254 (6 %)
MIRED_TOLERANCE = 15
XY_TOLERANCE = 0.06


def payload_xy(payload: Mapping[str, Any]) -> tuple[float, float] | None:
    """Return a ``/set`` payload's chromaticity (xy, rgb or colour temperature)."""
    color = payload.get("color")
    if isinstance(color, dict):
        if "x" in color and "y" in color:
            return (float(color["x"]), float(color["y"]))
        if all(k in color for k in ("r", "g", "b")):
            return rgb_to_xy((int(color["r"]), int(color["g"]), int(color["b"])))
    mired = payload.get("color_temp")
    if isinstance(mired, (int, float)) and mired > 0:
        return kelvin_to_xy(1e6 / mired)
    return None


def holds(frame: HueFrame, expected: Mapping[str, Any]) -> bool:
    """Return True when the bulb's reported look is within tolerance of ``expected``.

    A field the report or the payload leaves out is not compared. A bulb in
    the other colour mode (colour temperature vs xy) does not hold the look.
    """
    level = expected.get("brightness")
    if (
        frame.brightness is not None
        and isinstance(level, (int, float))
        and abs(frame.brightness - level) > BRIGHTNESS_TOLERANCE
    ):
        return False
    mired = expected.get("color_temp")
    if isinstance(mired, (int, float)):
        if frame.mirek is not None:
            return abs(frame.mirek - mired) <= MIRED_TOLERANCE
        return frame.xy is None  # in colour mode: not the white it should hold
    want = payload_xy(expected)
    if want is None:
        return True
    if frame.xy is not None:
        return math.dist(frame.xy, want) <= XY_TOLERANCE
    return frame.mirek is None  # in colour-temperature mode: not the colour
