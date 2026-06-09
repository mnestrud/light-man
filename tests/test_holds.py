"""Unit tests for the hold manager and the pure tap/state classifiers."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.light_man.holds import (
    HoldManager,
    action_to_op,
    detect_off_on,
)

from .conftest import FakeStore


def test_action_to_op() -> None:
    assert action_to_op("config_single") == "arm"
    assert action_to_op("config_double") == "arm"
    assert action_to_op("up_held") == "arm"
    assert action_to_op("down_held") == "arm"
    assert action_to_op("up_single") == "release"
    # Shade scenes / accent toggles must be ignored.
    assert action_to_op("up_double") is None
    assert action_to_op("down_triple") is None
    assert action_to_op("") is None


def test_detect_off_on() -> None:
    assert detect_off_on("OFF", "ON")
    assert not detect_off_on("ON", "ON")
    assert not detect_off_on(None, "ON")
    assert not detect_off_on("OFF", "OFF")


async def test_arm_release_changes_signal() -> None:
    holds = HoldManager(FakeStore(None))
    await holds.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)

    assert await holds.arm("living_room", armed_at=now, expires_at=later) is True
    assert holds.held_rooms() == {"living_room"}
    assert holds.is_held("living_room")
    # Re-arming an already-held room does not change the set.
    assert await holds.arm("living_room", armed_at=now, expires_at=later) is False

    assert await holds.release("living_room") is True
    assert await holds.release("living_room") is False
    assert holds.held_rooms() == set()


async def test_clear_returns_cleared_rooms() -> None:
    holds = HoldManager(FakeStore(None))
    await holds.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    await holds.arm("a", armed_at=now, expires_at=later)
    await holds.arm("b", armed_at=now, expires_at=later)
    assert sorted(await holds.clear()) == ["a", "b"]
    assert await holds.clear() == []  # idempotent


async def test_sweep_expired() -> None:
    holds = HoldManager(FakeStore(None))
    await holds.async_load()
    now = dt_util.utcnow()
    await holds.arm("past", armed_at=now, expires_at=now - timedelta(minutes=1))
    await holds.arm("future", armed_at=now, expires_at=now + timedelta(hours=8))
    assert await holds.sweep_expired(now) == ["past"]
    assert holds.held_rooms() == {"future"}
    assert await holds.sweep_expired(now) == []  # nothing more to sweep


async def test_load_persisted_and_skip_malformed() -> None:
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    data = {
        "good": {"armed_at": now.isoformat(), "expires_at": later.isoformat()},
        "bad": {"armed_at": "not-a-date", "expires_at": later.isoformat()},
    }
    holds = HoldManager(FakeStore(data))
    await holds.async_load()
    assert holds.held_rooms() == {"good"}
    assert holds.as_attributes() == {"good": later.isoformat()}


async def test_load_ignores_non_dict() -> None:
    holds = HoldManager(FakeStore("garbage"))
    await holds.async_load()
    assert holds.held_rooms() == set()
