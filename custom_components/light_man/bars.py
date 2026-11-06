"""Inovelli LED bars: per-room status lights, tap and shade animations, level.

Hass-free. The coordinator decides what each switch's bar should show; this
module turns that into Zigbee2MQTT commands and paces them (one unicast each).

Five things are written to a switch, all on ``<switch>/set``:

- **Level** — ``ledIntensityWhenOn`` follows the room's brightness curve, from
  ``BAR_PEAK_LEVEL`` at the curve's brightest down. Written only when it has
  moved ``BAR_LEVEL_STEP``, so a day is a handful of writes per switch.
- **Status** — one LED per state, lit while the state is true
  (``individual_led_effect``, indefinite): the top LED for a held look, the
  bottom LED for the hallway motion override. A tap that sets a state is
  answered on that LED itself: it pulses for ``FEEDBACK_S``, then stays on. An
  ignored config tap blinks the top LED red. One LED keeps the colour honest: a
  single LED beside the bar's blue reads as a warm white, which a whole-bar
  effect in the same colour does not (user, 2026-11-05).
- **Animations** — a timed whole-bar ``led_effect`` (shades, look released). It
  hides the status LEDs while it plays; they show again when it ends (checked
  on a VZM31-SN, 2026-11-05).

- **Start-up height** — ``defaultLevelLocal`` / ``defaultLevelRemote`` follow the
  level the switch's lights will wake at, so the bar comes on at the lights'
  height. A parameter write: it never switches anything and is safe while off.
- **Height** — the switch's own level (``brightness``), which the bar shows as
  its height. The switches have no level binding, so this moves the bar only.
  It is written only while the switch is on: a level written to an off switch
  turns the switch (not its lights) on, and the next paddle-on then does nothing
  (measured 2026-11-06).

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
KEY_DEFAULT_LOCAL = "defaultLevelLocal"
KEY_DEFAULT_REMOTE = "defaultLevelRemote"
KEY_HEIGHT = "brightness"  # the switch's own level: the bar's height
BAR_KEYS = frozenset(
    {KEY_LEVEL, KEY_EFFECT, KEY_LED, KEY_DEFAULT_LOCAL, KEY_DEFAULT_REMOTE}
)
DEFAULT_LEVEL_STEP = 8  # write the start-up height once it has moved this far
HEIGHT_STEP = 3  # correct the height once it is this far from the lights
HEIGHT_TRANSITION_S = 0.5  # the switch ramps its bar to the new height
HEIGHT_MIN, HEIGHT_MAX = 1, 254


def bar_level(fraction: float) -> int:
    """Return the bar's on-level for a curve fraction in [0, 1]."""
    return max(BAR_MIN_LEVEL, min(BAR_PEAK_LEVEL, round(BAR_PEAK_LEVEL * fraction)))


def scaled(level: int, fraction: float, floor: int) -> int:
    """Scale a status/animation level by the curve fraction, never below ``floor``."""
    return max(floor, min(100, round(level * fraction)))


def led_command(
    led: str,
    color: int | None,
    level: int,
    *,
    effect: str = "solid",
    duration: int = INDEFINITE,
) -> dict[str, Any]:
    """Return the command that lights one LED (solid by default) or clears it."""
    if color is None:
        cleared = {"led": led, "effect": "clear_effect"}
        return {KEY_LED: {**cleared, "color": 0, "level": 0, "duration": 0}}
    body = {"led": led, "effect": effect}
    return {KEY_LED: {**body, "color": color, "level": level, "duration": duration}}


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
        self._default: dict[str, int] = {}  # switch -> its start-up height
        self._leds: dict[str, dict[str, int | None]] = {}  # switch -> led -> colour
        self._status_level: dict[str, int] = {}  # switch -> level its status shows at
        self._announce: set[tuple[str, str]] = set()  # (switch, led) to pulse next
        self._settling: set[asyncio.Task[None]] = set()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    def seed_level(self, base: str, level: int) -> None:
        """Take the switch's own reported on-level as the start (first report only)."""
        self._level.setdefault(base, level)

    def seed_default(self, base: str, level: int) -> None:
        """Take the switch's own reported start-up height (first report only)."""
        self._default.setdefault(base, level)

    def default_level(self, base: str, level: int) -> None:
        """Keep the switch's start-up height at the level its lights wake at."""
        level = max(HEIGHT_MIN, min(HEIGHT_MAX, level))
        shown = self._default.get(base)
        if shown is None or abs(level - shown) >= DEFAULT_LEVEL_STEP:
            self._default[base] = level
            command = {KEY_DEFAULT_LOCAL: level, KEY_DEFAULT_REMOTE: level}
            self._queue(base, "default", command)

    def height(self, base: str, level: int) -> None:
        """Ramp the bar to ``level`` (the caller knows the switch is on)."""
        level = max(HEIGHT_MIN, min(HEIGHT_MAX, level))
        command = {KEY_HEIGHT: level, "transition": HEIGHT_TRANSITION_S}
        self._queue(base, "height", command)

    def forget(self, base: str) -> None:
        """Forget what a bar shows (a command failed): the next sync re-sends it."""
        self._level.pop(base, None)
        self._default.pop(base, None)
        self._leds.pop(base, None)
        self._status_level.pop(base, None)

    def announce(self, base: str, led: str) -> None:
        """Make the next sync pulse this status LED before it settles to solid."""
        self._announce.add((base, led))

    def unannounce(self, base: str) -> None:
        """Drop announcements a sync did not use (the tap changed nothing)."""
        self._announce = {key for key in self._announce if key[0] != base}

    def flash(
        self, base: str, led: str, effect: str, color: int, fraction: float
    ) -> None:
        """Show a timed effect on one LED, then put its status (if any) back."""
        level = scaled(FEEDBACK_LEVEL, fraction, STATUS_MIN_LEVEL)
        command = led_command(led, color, level, effect=effect, duration=FEEDBACK_S)
        self._queue(base, f"led{led}", command)
        self._settle(base, led)

    def sync(self, base: str, leds: dict[str, int | None], fraction: float) -> None:
        """Bring one bar to its level and status lights; send only what changed.

        ``leds`` maps each status LED this switch uses to its colour (None = off).
        An announced LED that is lit pulses first, then settles to solid.
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
        self._leds[base] = dict(leds)
        for led, color in leds.items():
            changed = before is None or before.get(led) != color
            # An announcement waits for the LED to light: a sync that runs while
            # the tap is still being handled must not use it up.
            announce = (base, led) in self._announce and color is not None
            if announce:
                self._announce.discard((base, led))
            if not (changed or announce or (relevel and color is not None)):
                continue
            self._status_level[base] = status_level
            if announce:
                # One second longer than the wait: the solid command replaces
                # the pulse, so the LED never drops out in between.
                pulse = led_command(
                    led,
                    color,
                    status_level,
                    effect=EFFECT_PULSE,
                    duration=FEEDBACK_S + 1,
                )
                self._queue(base, f"led{led}", pulse)
                self._settle(base, led)
            else:
                # The LED's new state: a solid still waiting from a pulse is stale.
                self._pending.pop((base, f"settle{led}"), None)
                self._queue(base, f"led{led}", led_command(led, color, status_level))

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

    def _settle(self, base: str, led: str) -> None:
        task = self._task_factory(self._settle_later(base, led))
        self._settling.add(task)
        task.add_done_callback(self._settling.discard)

    async def _settle_later(self, base: str, led: str) -> None:
        """After a pulse or flash, show the LED's status solid (if it has one)."""
        await self._sleep(FEEDBACK_S)
        color = self._leds.get(base, {}).get(led)
        if color is not None:
            # Its own slot: it queues behind a pulse that is still waiting,
            # rather than replacing it.
            level = self._status_level.get(base, STATUS_PEAK_LEVEL)
            self._queue(base, f"settle{led}", led_command(led, color, level))

    def cancel(self) -> None:
        """Drop everything pending and stop for good (unload)."""
        self._closed = True
        self._pending.clear()
        for task in list(self._settling):
            task.cancel()
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
