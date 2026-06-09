"""Per-room mode model + the pure action classifier.

Each room is ``adaptive`` (the default — no record) or held at a look
(``night`` / ``day`` / ``manual``). The mode is driven only by explicit Inovelli
action intents — never inferred from switch on/off bounce (the old, buggy
signal). Held modes persist across restarts (Store) so a restart never drops a
held look, and auto-expire at the next solar midnight.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.util import dt as dt_util

from .const import ACTION_MODE, HELD_KINDS, MODE_ADAPTIVE
from .models import HeldRecord

if TYPE_CHECKING:
    from datetime import datetime


def action_to_mode(action: str) -> str | None:
    """Map an Inovelli action string to a target mode, or None to ignore it.

    Config taps -> a held look; held-dim -> manual freeze; single taps ->
    ``adaptive`` (release). Double/triple taps (shade scenes) and anything else
    return None.
    """
    return ACTION_MODE.get(action)


class _StoreLike(Protocol):
    """The subset of ``helpers.storage.Store`` the mode manager uses."""

    async def async_load(self) -> Any: ...
    async def async_save(self, data: Any) -> None: ...


class ModeManager:
    """Persisted per-room held modes with set / release / TTL semantics."""

    def __init__(self, store: _StoreLike) -> None:
        """Bind to a Store; call :meth:`async_load` before use."""
        self._store = store
        self._held: dict[str, HeldRecord] = {}

    async def async_load(self) -> None:
        """Load persisted held modes, dropping malformed/unknown entries."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        for room, rec in data.items():
            kind = rec.get("kind")
            armed = dt_util.parse_datetime(rec.get("armed_at", ""))
            expires = dt_util.parse_datetime(rec.get("expires_at", ""))
            if kind not in HELD_KINDS or armed is None or expires is None:
                continue
            self._held[room] = HeldRecord(room, kind, armed, expires)

    async def _save(self) -> None:
        await self._store.async_save(
            {
                room: {
                    "kind": rec.kind,
                    "armed_at": rec.armed_at.isoformat(),
                    "expires_at": rec.expires_at.isoformat(),
                }
                for room, rec in self._held.items()
            }
        )

    def mode_of(self, room: str) -> str:
        """Return the room's current mode (a held kind, or ``adaptive``)."""
        rec = self._held.get(room)
        return rec.kind if rec is not None else MODE_ADAPTIVE

    def held_rooms(self) -> set[str]:
        """Return the set of rooms currently holding a look."""
        return set(self._held)

    def is_held(self, room: str) -> bool:
        """Return True if ``room`` is currently holding a look."""
        return room in self._held

    async def set_held(
        self, room: str, *, kind: str, armed_at: datetime, expires_at: datetime
    ) -> bool:
        """Hold a room at ``kind``. Returns True if the mode changed."""
        prev = self._held.get(room)
        changed = prev is None or prev.kind != kind
        self._held[room] = HeldRecord(room, kind, armed_at, expires_at)
        await self._save()
        return changed

    async def release(self, room: str) -> bool:
        """Release a room back to adaptive. Returns True if it was held."""
        if self._held.pop(room, None) is None:
            return False
        await self._save()
        return True

    async def clear(self) -> list[str]:
        """Release every held room. Returns the rooms that were cleared."""
        cleared = list(self._held)
        if cleared:
            self._held.clear()
            await self._save()
        return cleared

    async def sweep_expired(self, now: datetime) -> list[str]:
        """Release holds whose ``expires_at`` has passed. Returns those rooms."""
        expired = [r for r, rec in self._held.items() if rec.expires_at <= now]
        if expired:
            for room in expired:
                del self._held[room]
            await self._save()
        return expired

    def as_attributes(self) -> dict[str, dict[str, str]]:
        """Per-room ``{mode, expires_at}`` map for the sensor + diagnostics."""
        return {
            room: {"mode": rec.kind, "expires_at": rec.expires_at.isoformat()}
            for room, rec in self._held.items()
        }
