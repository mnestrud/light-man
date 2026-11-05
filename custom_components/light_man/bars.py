"""Inovelli LED bars: per-room status lights, tap and shade animations, level.

Hass-free. The coordinator decides what each switch's bar should show; this
module turns that into Zigbee2MQTT commands and paces them (one unicast each).

Three things are written to a bar, all on ``<switch>/set``:

- **Level** — ``ledIntensityWhenOn`` follows the room's brightness curve, from
  ``BAR_PEAK_LEVEL`` at the curve's brightest down. Written only when it has
  moved ``BAR_LEVEL_STEP``, so a day is a handful of writes per switch.
- **Status** — one LED per state, lit while the state is true
  (``individual_led_effect``, indefinite): the top LED for a held look, the
  bottom LED for the hallway motion override.
- **Animations** — a timed whole-bar ``led_effect`` (tap feedback, shades). It
  hides the status LEDs while it plays; they show again when it ends (checked
  on a VZM31-SN, 2026-11-05).

Status and animation brightness scale with the same curve fraction as the level.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from .const import HELD_DAY, HELD_MANUAL, HELD_NIGHT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

BAR_GAP_S = 0.2  # between two bar commands (unicasts)
BAR_PEAK_LEVEL = 25  # on-level at the curve's brightest (the bars' fixed level before)
BAR_MIN_LEVEL = 1
BAR_LEVEL_STEP = 2  # write the level only once it has moved this far
STATUS_PEAK_LEVEL = 60
STATUS_MIN_LEVEL = 10
STATUS_LEVEL_STEP = 5
EFFECT_MIN_LEVEL = 5
FEEDBACK_LEVEL = 60
# Shorter whole-bar effects do not show at all: 1 s and 2 s blinks and pulses
# were invisible on a VZM31-SN, 3 s ones showed (checked 2026-11-05).
FEEDBACK_S = 3
INDEFINITE = 255

# Inovelli hue wheel (0-255; 255 is white).
COLOR_RED = 0
COLOR_AMBER = 21
COLOR_GREEN = 85
COLOR_BAR = 170  # the bars' own resting colour
COLOR_VIOLET = 212
COLOR_WHITE = 255
LOOK_COLORS = {
    HELD_NIGHT: COLOR_AMBER,
    HELD_DAY: COLOR_WHITE,
    HELD_MANUAL: COLOR_VIOLET,
}

LED_TOP = "7"
LED_BOTTOM = "1"
EFFECT_PULSE = "pulse"
EFFECT_BLINK = "fast_blink"

KEY_LEVEL = "ledIntensityWhenOn"
KEY_EFFECT = "led_effect"
KEY_LED = "individual_led_effect"
BAR_KEYS = frozenset({KEY_LEVEL, KEY_EFFECT, KEY_LED})


def bar_level(fraction: float) -> int:
    """Return the bar's on-level for a curve fraction in [0, 1]."""
    return max(BAR_MIN_LEVEL, min(BAR_PEAK_LEVEL, round(BAR_PEAK_LEVEL * fraction)))


def scaled(level: int, fraction: float, floor: int) -> int:
    """Scale a status/animation level by the curve fraction, never below ``floor``."""
    return max(floor, min(100, round(level * fraction)))


def led_command(led: str, color: int | None, level: int) -> dict[str, Any]:
    """Return the command that lights one LED solid, or clears it (``color`` None)."""
    if color is None:
        effect = {"led": led, "effect": "clear_effect"}
        return {KEY_LED: {**effect, "color": 0, "level": 0, "duration": 0}}
    effect = {"led": led, "effect": "solid"}
    return {KEY_LED: {**effect, "color": color, "level": level, "duration": INDEFINITE}}


class BarManager:
    """Remember what each bar shows and send only what changed, paced."""

    def __init__(
        self,
        send_fn: Callable[[str, dict[str, Any]], Awaitable[bool]],
        task_factory: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        *,
        gap_s: float = BAR_GAP_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire collaborators; nothing runs until the first command."""
        self._send_fn = send_fn
        self._task_factory = task_factory
        self._gap_s = gap_s
        self._sleep = sleep
        # (switch, slot) -> command; a newer command for a slot replaces the older
        self._pending: dict[tuple[str, str], dict[str, Any]] = {}
        self._level: dict[str, int] = {}  # switch -> on-level it shows
        self._leds: dict[str, dict[str, int | None]] = {}  # switch -> led -> colour
        self._status_level: dict[str, int] = {}  # switch -> level its status shows at
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    def seed_level(self, base: str, level: int) -> None:
        """Take the switch's own reported on-level as the start (first report only)."""
        self._level.setdefault(base, level)

    def forget(self, base: str) -> None:
        """Forget what a bar shows (a command failed): the next sync re-sends it."""
        self._level.pop(base, None)
        self._leds.pop(base, None)
        self._status_level.pop(base, None)

    def sync(self, base: str, leds: dict[str, int | None], fraction: float) -> None:
        """Bring one bar to its level and status lights; send only what changed.

        ``leds`` maps each status LED this switch uses to its colour (None = off).
        """
        level = bar_level(fraction)
        shown = self._level.get(base)
        if shown is None or abs(level - shown) >= BAR_LEVEL_STEP:
            self._level[base] = level
            self._queue(base, "level", {KEY_LEVEL: level})
        status_level = scaled(STATUS_PEAK_LEVEL, fraction, STATUS_MIN_LEVEL)
        last_level = self._status_level.get(base, status_level)
        relevel = abs(status_level - last_level) >= STATUS_LEVEL_STEP
        before = self._leds.get(base)
        for led, color in leds.items():
            changed = before is None or before.get(led) != color
            if changed or (relevel and color is not None):
                self._queue(base, f"led{led}", led_command(led, color, status_level))
                self._status_level[base] = status_level
        self._leds[base] = dict(leds)

    def play(
        self,
        base: str,
        effect: str,
        color: int,
        level: int,
        duration: int,
        fraction: float,
    ) -> None:
        """Play a timed whole-bar animation (a newer one replaces a waiting one)."""
        body = {
            "effect": effect,
            "color": color,
            "level": scaled(level, fraction, EFFECT_MIN_LEVEL),
            "duration": duration,
        }
        self._queue(base, "effect", {KEY_EFFECT: body})

    def cancel(self) -> None:
        """Drop everything pending and stop for good (unload)."""
        self._closed = True
        self._pending.clear()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self._worker = None

    def _queue(self, base: str, slot: str, command: dict[str, Any]) -> None:
        if self._closed:
            return
        self._pending[(base, slot)] = command
        if self._worker is None or self._worker.done():
            self._worker = self._task_factory(self._run())

    async def _run(self) -> None:
        while self._pending:
            key = next(iter(self._pending))
            command = self._pending.pop(key)
            if not await self._send_fn(f"{key[0]}/set", command):
                self.forget(key[0])  # broker unavailable: re-sync later
            await self._sleep(self._gap_s)
