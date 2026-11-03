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
import contextlib
import itertools
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

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
    future: asyncio.Future[bool]
    queued_at: float
    origin_at: float | None  # when the triggering event arrived (latency stats)

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
        self.stats = PublisherStats()

    # --- public API -----------------------------------------------------------

    def enqueue(
        self,
        sends: Sequence[Send],
        *,
        priority: int,
        origin_at: float | None = None,
    ) -> asyncio.Future[bool]:
        """Queue a unit; the future is True once every send in it was acked.

        A send for a topic that is already pending in an older unit replaces it
        there (the older unit drops that send; an emptied unit resolves False).
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        sends = list(sends)
        if not sends:
            future.set_result(True)
            return future
        topics = {send.topic for send in sends}
        for unit in list(self._pending):
            kept = [send for send in unit.sends if send.topic not in topics]
            if len(kept) != len(unit.sends):
                self.stats.coalesced += len(unit.sends) - len(kept)
                unit.sends = kept
                if not kept:
                    self._pending.remove(unit)
                    if not unit.future.done():
                        unit.future.set_result(False)
        unit = _Unit(
            seq=next(self._seq),
            priority=priority,
            sends=sends,
            future=future,
            queued_at=self._clock(),
            origin_at=origin_at,
        )
        self._pending.append(unit)
        self.stats.queued += 1
        if self._worker is None or self._worker.done():
            self._worker = self._task_factory(self._run())
        return future

    def cancel(self) -> None:
        """Drop everything pending and stop the worker (unload)."""
        for unit in self._pending:
            if not unit.future.done():
                unit.future.set_result(False)
        self._pending.clear()
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
            waited = await self._wait_for_room(unit)
            if waited:
                continue  # re-pick: something higher priority may have arrived
            self._pending.remove(unit)
            ok = True
            for send in unit.sends:
                ok = await self._send_one(send, unit.priority) and ok
            if unit.origin_at is not None and unit.priority == PRIORITY_INTERACTIVE:
                self.stats.interactive_latency_s.append(self._clock() - unit.origin_at)
            if not unit.future.done():
                unit.future.set_result(ok)

    async def _wait_for_room(self, unit: _Unit) -> bool:
        """Sleep once if the unit does not fit the budget now; True if it slept."""
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
            limit = max(self._tick_budget, need)
            if ticks + need > limit:
                wait = max(
                    wait,
                    self._wait_until_free(
                        now, ticks + need - limit, any_priority=False
                    ),
                )
        if self._last_send_at is not None:
            wait = max(wait, self._last_send_at + self._min_gap_s - now)
        if wait <= 0:
            return False
        self.stats.paced_waits += 1
        await self._sleep(wait)
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
        with contextlib.suppress(Exception):
            ok = bool(await self._send_fn(send.topic, send.payload))
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
