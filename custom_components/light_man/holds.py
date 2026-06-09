"""Hold model + the pure tap/state classifiers.

A "hold" marks a room whose manual scene must survive the next adaptive push.
Holds persist across restarts (Store) so a restart never re-clobbers a held
scene. ``action_to_op`` / ``detect_off_on`` are pure so the tap-subscription
wiring can be tested without hass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.util import dt as dt_util

from .const import (
    ACTIONS_ARM,
    ACTIONS_RELEASE,
    OP_ARM,
    OP_RELEASE,
    STATE_OFF,
    STATE_ON,
)
from .models import HoldRecord

if TYPE_CHECKING:
    from datetime import datetime


def action_to_op(action: str) -> str | None:
    """Map an Inovelli ``action`` string to a hold op, or None to ignore it.

    Double/triple taps (PowerView shade scenes) and everything else return None.
    """
    if action in ACTIONS_ARM:
        return OP_ARM
    if action in ACTIONS_RELEASE:
        return OP_RELEASE
    return None


def detect_off_on(prev: str | None, new: str | None) -> bool:
    """Return True when a switch went OFF -> ON (a room turn-on = release)."""
    return prev == STATE_OFF and new == STATE_ON


class _StoreLike(Protocol):
    """The subset of ``helpers.storage.Store`` the hold manager uses."""

    async def async_load(self) -> Any: ...
    async def async_save(self, data: Any) -> None: ...


class HoldManager:
    """Persisted set of room holds with arm / release / TTL semantics."""

    def __init__(self, store: _StoreLike) -> None:
        """Bind to a Store; call :meth:`async_load` before use."""
        self._store = store
        self._holds: dict[str, HoldRecord] = {}

    async def async_load(self) -> None:
        """Load persisted holds, dropping any malformed entries."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        for room, rec in data.items():
            armed = dt_util.parse_datetime(rec.get("armed_at", ""))
            expires = dt_util.parse_datetime(rec.get("expires_at", ""))
            if armed is None or expires is None:
                continue
            self._holds[room] = HoldRecord(room, armed, expires)

    async def _save(self) -> None:
        await self._store.async_save(
            {
                room: {
                    "armed_at": rec.armed_at.isoformat(),
                    "expires_at": rec.expires_at.isoformat(),
                }
                for room, rec in self._holds.items()
            }
        )

    def held_rooms(self) -> set[str]:
        """Return the set of currently held rooms."""
        return set(self._holds)

    def is_held(self, room: str) -> bool:
        """Return True if ``room`` currently has a hold."""
        return room in self._holds

    async def arm(self, room: str, *, armed_at: datetime, expires_at: datetime) -> bool:
        """Arm (or refresh) a room hold. Returns True if the held set changed."""
        changed = room not in self._holds
        self._holds[room] = HoldRecord(room, armed_at, expires_at)
        await self._save()
        return changed

    async def release(self, room: str) -> bool:
        """Release a room hold. Returns True if it was held."""
        if self._holds.pop(room, None) is None:
            return False
        await self._save()
        return True

    async def clear(self) -> list[str]:
        """Release all holds. Returns the rooms that were cleared."""
        cleared = list(self._holds)
        if cleared:
            self._holds.clear()
            await self._save()
        return cleared

    async def sweep_expired(self, now: datetime) -> list[str]:
        """Remove holds whose ``expires_at`` has passed. Returns those rooms."""
        expired = [r for r, rec in self._holds.items() if rec.expires_at <= now]
        if expired:
            for room in expired:
                del self._holds[room]
            await self._save()
        return expired

    def as_attributes(self) -> dict[str, str]:
        """Per-room ``expires_at`` map for the active-holds sensor attributes."""
        return {room: rec.expires_at.isoformat() for room, rec in self._holds.items()}
