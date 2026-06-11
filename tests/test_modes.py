"""Unit tests for the mode manager and the pure action classifier."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.light_man.modes import ModeManager, action_to_mode

from .conftest import FakeStore


def test_action_to_mode() -> None:
    assert action_to_mode("config_single") == "night"
    assert action_to_mode("config_double") == "day"
    assert action_to_mode("up_held") == "manual"
    assert action_to_mode("down_held") == "manual"
    # Both single taps release to adaptive.
    assert action_to_mode("up_single") == "adaptive"
    assert action_to_mode("down_single") == "adaptive"
    # Shade scenes and anything else are ignored.
    assert action_to_mode("up_double") is None
    assert action_to_mode("down_triple") is None
    assert action_to_mode("") is None


async def test_set_held_and_mode_of() -> None:
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)

    assert modes.mode_of("office") == "adaptive"
    assert await modes.set_held("office", kind="night", armed_at=now, expires_at=later)
    assert modes.mode_of("office") == "night"
    assert modes.is_held("office")
    assert modes.held_rooms() == {"office"}

    # Re-holding the same kind does not count as a change.
    assert not await modes.set_held(
        "office", kind="night", armed_at=now, expires_at=later
    )
    # A different kind is a change.
    assert await modes.set_held("office", kind="day", armed_at=now, expires_at=later)
    assert modes.mode_of("office") == "day"


async def test_release_and_clear() -> None:
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    await modes.set_held("a", kind="night", armed_at=now, expires_at=later)
    await modes.set_held("b", kind="day", armed_at=now, expires_at=later)

    assert await modes.release("a") is True
    assert await modes.release("a") is False
    assert await modes.clear() == ["b"]
    assert await modes.clear() == []


async def test_sweep_expired() -> None:
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    now = dt_util.utcnow()
    await modes.set_held(
        "past", kind="night", armed_at=now, expires_at=now - timedelta(minutes=1)
    )
    await modes.set_held(
        "future", kind="day", armed_at=now, expires_at=now + timedelta(hours=8)
    )
    assert await modes.sweep_expired(now) == ["past"]
    assert modes.held_rooms() == {"future"}
    assert await modes.sweep_expired(now) == []


async def test_load_persisted_and_skip_malformed() -> None:
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    data = {
        "good": {
            "kind": "night",
            "armed_at": now.isoformat(),
            "expires_at": later.isoformat(),
        },
        "bad_kind": {
            "kind": "rainbow",
            "armed_at": now.isoformat(),
            "expires_at": later.isoformat(),
        },
        "bad_date": {
            "kind": "day",
            "armed_at": "nope",
            "expires_at": later.isoformat(),
        },
    }
    modes = ModeManager(FakeStore(data))
    await modes.async_load()
    assert modes.held_rooms() == {"good"}
    assert modes.as_attributes() == {
        "good": {"mode": "night", "expires_at": later.isoformat()}
    }


async def test_load_ignores_non_dict() -> None:
    modes = ModeManager(FakeStore("garbage"))
    await modes.async_load()
    assert modes.held_rooms() == set()


async def test_prune_drops_orphaned_keys() -> None:
    modes = ModeManager(FakeStore(None))
    await modes.async_load()
    now = dt_util.utcnow()
    later = now + timedelta(hours=8)
    await modes.set_held(
        "kitchen.overhead", kind="night", armed_at=now, expires_at=later
    )
    await modes.set_held("overhead_bath", kind="night", armed_at=now, expires_at=later)

    # Only keys that are still valid hold targets survive a prune.
    assert await modes.prune({"kitchen.overhead"}) == ["overhead_bath"]
    assert modes.held_rooms() == {"kitchen.overhead"}
    # Idempotent: nothing left to prune.
    assert await modes.prune({"kitchen.overhead"}) == []
