"""Tests for the paced publisher (no hass; fake clock)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.light_man.publisher import (
    PRIORITY_INTERACTIVE,
    PRIORITY_TICK,
    PacedPublisher,
    Send,
)


class FakeTime:
    """A clock that only moves when the publisher sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)
        await asyncio.sleep(0)


class Harness:
    """Publisher wired to a recording send function."""

    def __init__(
        self, *, fail: set[str] | None = None, raise_on: set[str] | None = None
    ) -> None:
        self.time = FakeTime()
        self.sent: list[tuple[float, str]] = []
        self.fail = fail or set()
        self.raise_on = raise_on or set()
        self.pub = PacedPublisher(
            self._send,
            asyncio.ensure_future,
            is_group=lambda topic: topic.startswith("g/"),
            min_gap_s=0.15,
            clock=self.time.clock,
            sleep=self.time.sleep,
        )

    async def _send(self, topic: str, _payload: dict[str, Any]) -> bool:
        if topic in self.raise_on:
            raise RuntimeError("broker down")
        self.sent.append((self.time.now, topic))
        return topic not in self.fail

    def offsets(self) -> list[tuple[float, str]]:
        start = self.sent[0][0]
        return [(round(t - start, 2), topic) for t, topic in self.sent]


def _s(topic: str) -> Send:
    return Send(topic, {"brightness": 1})


async def _drain(h: Harness) -> None:
    for _ in range(500):
        await asyncio.sleep(0)
        if not h.pub._pending and (h.pub._worker is None or h.pub._worker.done()):
            return


async def test_interactive_before_tick_and_fifo_within_priority() -> None:
    h = Harness()
    h.pub.enqueue([_s("d/a")], priority=PRIORITY_TICK)
    h.pub.enqueue([_s("d/b")], priority=PRIORITY_TICK)
    h.pub.enqueue([_s("d/tap")], priority=PRIORITY_INTERACTIVE)
    await _drain(h)
    assert [topic for _t, topic in h.sent] == ["d/tap", "d/a", "d/b"]


async def test_unicasts_pay_only_min_gap() -> None:
    h = Harness()
    for i in range(10):
        h.pub.enqueue([_s(f"d/{i}")], priority=PRIORITY_TICK)
    await _drain(h)
    times = [t for t, _topic in h.offsets()]
    assert times[-1] == pytest.approx(9 * 0.15)


async def test_tick_budget_defers_seventh_group_send_to_window_end() -> None:
    h = Harness()
    futures = [h.pub.enqueue([_s(f"g/{i}")], priority=PRIORITY_TICK) for i in range(7)]
    await _drain(h)
    offsets = h.offsets()
    assert offsets[5][0] == pytest.approx(5 * 0.15)
    assert offsets[6][0] >= 10.0  # waited for the first stamp to leave the window
    assert all(f.result() for f in futures)
    assert h.pub.stats.max_group_sends_in_window == 6
    assert h.pub.stats.paced_waits >= 1


async def test_interactive_uses_reserved_slots_then_waits_at_cap() -> None:
    h = Harness()
    for i in range(6):
        h.pub.enqueue([_s(f"g/t{i}")], priority=PRIORITY_TICK)
    await _drain(h)
    for i in range(3):
        h.pub.enqueue([_s(f"g/i{i}")], priority=PRIORITY_INTERACTIVE)
    await _drain(h)
    offsets = dict((topic, t) for t, topic in h.offsets())
    assert offsets["g/i0"] < 2.0  # slot 7: immediate
    assert offsets["g/i1"] < 2.0  # slot 8: immediate
    assert offsets["g/i2"] >= 10.0  # cap reached: waits for the window
    assert h.pub.group_sends_in_window() <= 8


async def test_unit_is_sent_contiguously_in_order() -> None:
    h = Harness()
    h.pub.enqueue([_s("g/flood"), _s("g/reassert")], priority=PRIORITY_TICK)
    h.pub.enqueue([_s("d/other")], priority=PRIORITY_TICK)
    await _drain(h)
    assert [topic for _t, topic in h.sent] == ["g/flood", "g/reassert", "d/other"]


async def test_unit_is_budget_checked_as_a_whole() -> None:
    h = Harness()
    for i in range(5):
        h.pub.enqueue([_s(f"g/t{i}")], priority=PRIORITY_TICK)
    await _drain(h)
    h.pub.enqueue([_s("g/flood"), _s("g/reassert")], priority=PRIORITY_TICK)
    await _drain(h)
    offsets = dict((topic, t) for t, topic in h.offsets())
    assert offsets["g/flood"] >= 10.0  # 5 + 2 > 6: the whole unit waits
    assert offsets["g/reassert"] - offsets["g/flood"] == pytest.approx(0.15)


async def test_oversized_unit_sends_once_window_clears() -> None:
    h = Harness()
    sends = [_s(f"g/{i}") for i in range(7)]
    future = h.pub.enqueue(sends, priority=PRIORITY_TICK)
    await _drain(h)
    assert future.result() is True
    assert len(h.sent) == 7


async def test_coalescing_replaces_pending_send_and_resolves_emptied_unit() -> None:
    h = Harness()
    for i in range(6):
        h.pub.enqueue([_s(f"g/t{i}")], priority=PRIORITY_TICK)
    stale = h.pub.enqueue([_s("g/room")], priority=PRIORITY_TICK)
    fresh = h.pub.enqueue(
        [Send("g/room", {"brightness": 2})], priority=PRIORITY_INTERACTIVE
    )
    await _drain(h)
    assert stale.result() is False
    assert fresh.result() is True
    assert [topic for _t, topic in h.sent].count("g/room") == 1
    assert h.pub.stats.coalesced == 1


async def test_partial_coalescing_keeps_the_rest_of_the_unit() -> None:
    h = Harness()
    h.pub.enqueue([_s("d/x")], priority=PRIORITY_TICK)
    unit = h.pub.enqueue([_s("g/a"), _s("g/b")], priority=PRIORITY_TICK)
    h.pub.enqueue([_s("g/b")], priority=PRIORITY_TICK)
    await _drain(h)
    assert unit.result() is True
    assert [topic for _t, topic in h.sent] == ["d/x", "g/a", "g/b"]


async def test_failures_resolve_false_and_are_counted() -> None:
    h = Harness(fail={"d/bad"}, raise_on={"d/boom"})
    bad = h.pub.enqueue([_s("d/bad")], priority=PRIORITY_TICK)
    boom = h.pub.enqueue([_s("d/boom")], priority=PRIORITY_TICK)
    good = h.pub.enqueue([_s("d/ok")], priority=PRIORITY_TICK)
    await _drain(h)
    assert bad.result() is False
    assert boom.result() is False
    assert good.result() is True
    stats = h.pub.stats.as_dict()
    assert stats["failed"] == 2
    assert stats["sent"] == 1
    assert stats["sends_by_topic"] == {"d/ok": 1}


async def test_empty_unit_resolves_true_without_sending() -> None:
    h = Harness()
    assert h.pub.enqueue([], priority=PRIORITY_TICK).result() is True
    assert h.sent == []


async def test_cancel_resolves_pending_false() -> None:
    h = Harness()
    first = h.pub.enqueue([_s("g/0")], priority=PRIORITY_TICK)
    second = h.pub.enqueue([_s("g/1")], priority=PRIORITY_TICK)
    h.pub.cancel()  # before the worker runs
    assert first.result() is False
    assert second.result() is False
    await _drain(h)
    assert h.sent == []
    h.pub.cancel()  # idempotent with no worker


async def test_interactive_latency_recorded() -> None:
    h = Harness()
    origin = h.time.now
    h.pub.enqueue([_s("d/x")], priority=PRIORITY_INTERACTIVE, origin_at=origin)
    h.pub.enqueue([_s("d/y")], priority=PRIORITY_INTERACTIVE, origin_at=origin)
    await _drain(h)
    latency = h.pub.stats.as_dict()["interactive_latency_ms"]
    assert latency["p50_ms"] is not None
    assert latency["p95_ms"] == pytest.approx(150.0)


async def test_stats_without_latency() -> None:
    stats = Harness().pub.stats.as_dict()
    assert stats["interactive_latency_ms"] == {"p50_ms": None, "p95_ms": None}
    assert stats["queued"] == 0
