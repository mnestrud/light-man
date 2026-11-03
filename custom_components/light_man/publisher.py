"""Paced MQTT publisher: one queue for every Light Man send.

Zigbee group commands are network broadcasts and share the coordinator's
broadcast table (about 8 entries per ~9 s on this mesh); zigbee-herdsman never
retries a groupcast rejected with ``BUSY``. Every publish path therefore goes
through this queue, which:

- sends **units** (a list of sends, e.g. a source's flood plus its held-room
  re-asserts) contiguously and in order, budget-checked as a whole;
- keeps tick traffic to ``tick_budget`` group sends per ``window_s`` and all
  traffic to ``cap``, leaving the difference for interactive sends and the
  mesh's own broadcasts;
- serves interactive units before tick units, and lets a newer send for a topic
  replace a pending older one (coalescing);
- spaces any two sends by ``min_gap_s``.

Hass-free: the coordinator injects the send coroutine, a task factory, the
group classifier, and (in tests) a clock and sleep.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

_LOGGER = logging.getLogger(__name__)

PRIORITY_INTERACTIVE = 0
PRIORITY_TICK = 1

BROADCAST_WINDOW_S = 10.0
BROADCAST_CAP = 8
TICK_BUDGET = 6
LATENCY_SAMPLES = 200


@dataclass(frozen=True)
class Send:
    """One MQTT publish: a topic and its JSON payload."""

    topic: str
    payload: dict[str, Any]


@dataclass
class _Unit:
    seq: int
    priority: int
    sends: list[Send]
    future: asyncio.Future[list[Send]]
    queued_at: float
    origin_at: float | None  # when the triggering event arrived (latency stats)
    planner: Callable[[], list[Send]] | None = None  # lazy: plan at dequeue
    key: str | None = None  # lazy units with the same key replace each other

    def sort_key(self) -> tuple[int, int]:
        return (self.priority, self.seq)


@dataclass
class PublisherStats:
    """Counters exposed in diagnostics and the panel."""

    queued: int = 0
    sent: int = 0
    failed: int = 0
    coalesced: int = 0
    paced_waits: int = 0
    max_group_sends_in_window: int = 0
    sends_by_topic: dict[str, int] = field(default_factory=dict)
    interactive_latency_s: deque[float] = field(
        default_factory=lambda: deque(maxlen=LATENCY_SAMPLES)
    )

    def as_dict(self) -> dict[str, Any]:
        """JSON-able view (latency as p50/p95 milliseconds)."""
        samples = sorted(self.interactive_latency_s)
        latency: dict[str, float | None] = {"p50_ms": None, "p95_ms": None}
        if samples:
            latency["p50_ms"] = round(samples[len(samples) // 2] * 1000, 1)
            p95_index = min(len(samples) - 1, round(0.95 * (len(samples) - 1)))
            latency["p95_ms"] = round(samples[p95_index] * 1000, 1)
        return {
            "queued": self.queued,
            "sent": self.sent,
            "failed": self.failed,
            "coalesced": self.coalesced,
            "paced_waits": self.paced_waits,
            "max_group_sends_in_window": self.max_group_sends_in_window,
            "sends_by_topic": dict(self.sends_by_topic),
            "interactive_latency_ms": latency,
        }


class PacedPublisher:
    """Budgeted, prioritised, coalescing publish queue."""

    def __init__(  # injected collaborators keep it hass-free
        self,
        send_fn: Callable[[str, dict[str, Any]], Awaitable[bool]],
        task_factory: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        *,
        is_group: Callable[[str], bool],
        cap: int = BROADCAST_CAP,
        tick_budget: int = TICK_BUDGET,
        window_s: float = BROADCAST_WINDOW_S,
        min_gap_s: float = 0.15,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wire collaborators; nothing runs until the first ``enqueue``."""
        self._send_fn = send_fn
        self._task_factory = task_factory
        self._is_group = is_group
        self._cap = cap
        self._tick_budget = min(tick_budget, cap)
        self._window_s = window_s
        self._min_gap_s = min_gap_s
        self._clock = clock
        self._sleep = sleep
        self._pending: list[_Unit] = []
        self._seq = itertools.count()
        self._group_sends: deque[tuple[float, int]] = deque()  # (time, priority)
        self._last_send_at: float | None = None
        self._worker: asyncio.Task[None] | None = None
        self._current: _Unit | None = None
        self._closed = False
        self._wake = asyncio.Event()
        self.stats = PublisherStats()

    # --- public API -----------------------------------------------------------

    def enqueue(
        self,
        sends: Sequence[Send],
        *,
        priority: int,
        origin_at: float | None = None,
    ) -> asyncio.Future[list[Send]]:
        """Queue a unit; the future resolves to the sends the broker acked.

        A send for a topic that is already pending in an older unit replaces it
        there (the older unit drops that send; an emptied unit resolves ``[]``).
        """
        future = self._new_future()
        sends = list(sends)
        if not sends:
            future.set_result([])
            return future
        self._coalesce({send.topic for send in sends})
        self._add(
            _Unit(next(self._seq), priority, sends, future, self._clock(), origin_at)
        )
        return future

    def enqueue_planned(
        self,
        key: str,
        planner: Callable[[], list[Send]],
        *,
        priority: int,
    ) -> asyncio.Future[list[Send]]:
        """Queue a unit whose sends are planned when the worker takes it.

        Tick floods use this so a unit always reflects the holds and curve at the
        moment it is sent (a hold armed while the unit waited is re-asserted).
        A pending planned unit with the same ``key`` is replaced.
        """
        future = self._new_future()
        for unit in list(self._pending):
            if unit.key == key:
                self._pending.remove(unit)
                self.stats.coalesced += 1
                if not unit.future.done():
                    unit.future.set_result([])
        unit = _Unit(next(self._seq), priority, [], future, self._clock(), None)
        unit.planner = planner
        unit.key = key
        self._add(unit)
        return future

    def _new_future(self) -> asyncio.Future[list[Send]]:
        return asyncio.get_running_loop().create_future()

    def _coalesce(self, topics: set[str]) -> None:
        for unit in list(self._pending):
            if unit.planner is not None:
                continue
            kept = [send for send in unit.sends if send.topic not in topics]
            if len(kept) != len(unit.sends):
                self.stats.coalesced += len(unit.sends) - len(kept)
                unit.sends = kept
                if not kept:
                    self._pending.remove(unit)
                    if not unit.future.done():
                        unit.future.set_result([])

    def _add(self, unit: _Unit) -> None:
        if self._closed:
            unit.future.set_result([])
            return
        self._pending.append(unit)
        self.stats.queued += 1
        self._wake.set()
        if self._worker is None or self._worker.done():
            self._worker = self._task_factory(self._run())

    def cancel(self) -> None:
        """Drop everything pending and stop the worker for good (unload)."""
        self._closed = True
        units = [*self._pending, *([self._current] if self._current else [])]
        for unit in units:
            if not unit.future.done():
                unit.future.set_result([])
        self._pending.clear()
        self._current = None
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        self._worker = None

    def group_sends_in_window(self, now: float | None = None) -> int:
        """Group sends in the current rolling window."""
        self._expire(self._clock() if now is None else now)
        return len(self._group_sends)

    # --- worker -----------------------------------------------------------------

    async def _run(self) -> None:
        while self._pending:
            unit = min(self._pending, key=_Unit.sort_key)
            if not self._plan(unit):
                continue
            if await self._wait_for_room(unit):
                continue  # re-pick: a planned unit re-plans, a tap may now go first
            self._pending.remove(unit)
            self._current = unit
            acked = [s for s in unit.sends if await self._send_one(s, unit.priority)]
            self._current = None
            if unit.origin_at is not None and unit.priority == PRIORITY_INTERACTIVE:
                self.stats.interactive_latency_s.append(self._clock() - unit.origin_at)
            if not unit.future.done():
                unit.future.set_result(acked)

    def _plan(self, unit: _Unit) -> bool:
        """(Re-)plan a planned unit; False (and resolved) if it has nothing to send.

        Planned units re-plan on every pick, so a unit that waited for budget
        still reflects the holds and curve at send time.
        """
        if unit.planner is None:
            return True
        try:
            unit.sends = list(unit.planner())
        except Exception:
            _LOGGER.exception("planning a publish unit failed")
            unit.sends = []
        if unit.sends:
            return True
        self._pending.remove(unit)
        if not unit.future.done():
            unit.future.set_result([])
        return False

    async def _wait_for_room(self, unit: _Unit) -> bool:
        """Wait if the unit does not fit the budget now; True if it waited.

        The wait ends early when anything new is enqueued, so an interactive
        unit never sits behind a tick unit that is waiting for budget.
        """
        now = self._clock()
        self._expire(now)
        groups = sum(1 for send in unit.sends if self._is_group(send.topic))
        need = min(groups, self._cap)
        total = len(self._group_sends)
        wait = 0.0
        if need and total + need > self._cap:
            wait = self._wait_until_free(
                now, total + need - self._cap, any_priority=True
            )
        if need and unit.priority == PRIORITY_TICK:
            ticks = sum(1 for _t, p in self._group_sends if p == PRIORITY_TICK)
            if need > self._tick_budget:
                # An oversized tick unit waits for earlier tick sends to leave the
                # window (taps do not hold it back; the cap rule above still
                # applies). The coordinator keeps tick units within the budget.
                if ticks:
                    wait = max(
                        wait, self._wait_until_free(now, ticks, any_priority=False)
                    )
            elif ticks + need > self._tick_budget:
                wait = max(
                    wait,
                    self._wait_until_free(
                        now, ticks + need - self._tick_budget, any_priority=False
                    ),
                )
        if self._last_send_at is not None:
            wait = max(wait, self._last_send_at + self._min_gap_s - now)
        if wait <= 0:
            return False
        self.stats.paced_waits += 1
        self._wake.clear()
        sleeper = asyncio.ensure_future(self._sleep(wait))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, waker):
                if not task.done():
                    task.cancel()
        return True

    def _wait_until_free(self, now: float, slots: int, *, any_priority: bool) -> float:
        stamps = [t for t, p in self._group_sends if any_priority or p == PRIORITY_TICK]
        # Callers only ask when the window is over budget, so stamps is non-empty.
        oldest_needed = stamps[min(slots, len(stamps)) - 1]
        return max(0.0, oldest_needed + self._window_s - now)

    async def _send_one(self, send: Send, priority: int) -> bool:
        now = self._clock()
        if self._last_send_at is not None:
            gap = self._last_send_at + self._min_gap_s - now
            if gap > 0:
                await self._sleep(gap)
                now = self._clock()
        ok = False
        try:
            ok = bool(await self._send_fn(send.topic, send.payload))
        except Exception:
            _LOGGER.exception("publish to %s failed", send.topic)
        self._last_send_at = self._clock()
        if self._is_group(send.topic):
            self._group_sends.append((self._last_send_at, priority))
            self._expire(self._last_send_at)
            self.stats.max_group_sends_in_window = max(
                self.stats.max_group_sends_in_window, len(self._group_sends)
            )
        if ok:
            self.stats.sent += 1
            by_topic = self.stats.sends_by_topic
            by_topic[send.topic] = by_topic.get(send.topic, 0) + 1
        else:
            self.stats.failed += 1
        return ok

    def _expire(self, now: float) -> None:
        while self._group_sends and now - self._group_sends[0][0] >= self._window_s:
            self._group_sends.popleft()
