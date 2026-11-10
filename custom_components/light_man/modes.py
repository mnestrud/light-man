"""Per-room mode model + the pure action classifier.

Each room is ``adaptive`` (the default — no record) or held at a look
(``night`` / ``day`` / ``manual``). The mode is driven only by explicit Inovelli
action intents — never inferred from switch on/off bounce (the old, buggy
signal). Held modes persist across restarts (Store) so a restart never drops a
held look, and auto-expire at the next local midnight (00:00).

Zone overrides (a tap holding an occupancy zone on, motion ignored) persist in
the same Store under ``OVERRIDES_KEY``, also expiring at 00:00. Zones whose
motion is switched off (a config hold) persist under ``MOTION_OFF_KEY`` with no
expiry: only another config hold turns their motion back on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from homeassistant.util import dt as dt_util

from .const import (
    ACTION_MODE,
    HELD_KINDS,
    MODE_ADAPTIVE,
    MODE_MOTION_OFF,
    MODE_OVERRIDE,
)
from .models import HeldRecord

if TYPE_CHECKING:
    from datetime import datetime

OVERRIDES_KEY = "__zone_overrides__"
MOTION_OFF_KEY = "__zone_motion_off__"


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
        self._overrides: dict[str, datetime] = {}  # zone -> expires_at
        self._motion_off: set[str] = set()  # zones whose motion is switched off

    async def async_load(self) -> None:
        """Load persisted held modes, dropping malformed/unknown entries."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return
        data = dict(data)
        motion_off = data.pop(MOTION_OFF_KEY, None)
        if isinstance(motion_off, list):
            self._motion_off = {zone for zone in motion_off if isinstance(zone, str)}
        overrides = data.pop(OVERRIDES_KEY, None)
        if isinstance(overrides, dict):
            for zone, expires_raw in overrides.items():
                expires = dt_util.parse_datetime(str(expires_raw))
                if expires is not None:
                    self._overrides[zone] = expires
        for room, rec in data.items():
            if not isinstance(rec, dict):
                continue
            kind = rec.get("kind")
            armed = dt_util.parse_datetime(rec.get("armed_at", ""))
            expires = dt_util.parse_datetime(rec.get("expires_at", ""))
            if kind not in HELD_KINDS or armed is None or expires is None:
                continue
            look = rec.get("look")
            self._held[room] = HeldRecord(
                room, kind, armed, expires, look if isinstance(look, dict) else None
            )

    async def _save(self) -> None:
        """Persist without delaying the caller's send (review item 14).

        HA's ``Store.async_delay_save`` schedules the write (and flushes it at
        shutdown), so a tap's frame leaves before the disk write.
        """
        delay_save = getattr(self._store, "async_delay_save", None)
        if delay_save is not None:
            delay_save(self._data, 0)
            return
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            room: {
                "kind": rec.kind,
                "armed_at": rec.armed_at.isoformat(),
                "expires_at": rec.expires_at.isoformat(),
                **({"look": rec.look} if rec.look is not None else {}),
            }
            for room, rec in self._held.items()
        }
        if self._motion_off:
            data[MOTION_OFF_KEY] = sorted(self._motion_off)
        if self._overrides:
            data[OVERRIDES_KEY] = {
                zone: expires.isoformat() for zone, expires in self._overrides.items()
            }
        return data

    # --- zone motion off (config hold) -------------------------------------

    def motion_off(self, zone: str) -> bool:
        """Return True while the zone's motion is switched off."""
        return zone in self._motion_off

    async def set_motion_off(self, zone: str, *, off: bool) -> None:
        """Switch a zone's motion off (or back on); persisted, no expiry."""
        if off:
            self._motion_off.add(zone)
        else:
            self._motion_off.discard(zone)
        await self._save()

    # --- zone overrides -----------------------------------------------------

    def override_until(self, zone: str) -> datetime | None:
        """When the zone's override expires, or None if it has none."""
        return self._overrides.get(zone)

    def overrides(self) -> dict[str, datetime]:
        """Every active zone override and its expiry."""
        return dict(self._overrides)

    async def set_override(self, zone: str, *, expires_at: datetime) -> None:
        """Arm (or extend) a zone override."""
        self._overrides[zone] = expires_at
        await self._save()

    async def end_override(self, zone: str) -> bool:
        """End a zone override. Returns True if there was one."""
        if self._overrides.pop(zone, None) is None:
            return False
        await self._save()
        return True

    async def sweep_expired_overrides(self, now: datetime) -> list[str]:
        """End overrides whose expiry has passed. Returns those zones."""
        expired = [z for z, expires in self._overrides.items() if expires <= now]
        if expired:
            for zone in expired:
                del self._overrides[zone]
            await self._save()
        return expired

    def mode_of(self, room: str) -> str:
        """Return the room's current mode (a held kind, or ``adaptive``)."""
        rec = self._held.get(room)
        return rec.kind if rec is not None else MODE_ADAPTIVE

    def look_of(self, room: str) -> dict[str, Any] | None:
        """Return a manual hold's captured look (persisted across restarts)."""
        rec = self._held.get(room)
        return rec.look if rec is not None else None

    async def set_look(self, room: str, look: dict[str, Any]) -> bool:
        """Store the captured look on a held room. False if it is not held."""
        rec = self._held.get(room)
        if rec is None:
            return False
        rec.look = dict(look)
        await self._save()
        return True

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

    async def prune(self, valid: set[str]) -> list[str]:
        """Drop holds whose key is no longer a valid target. Returns the dropped.

        Run after a topology change (e.g. the v8 light-ref rename): a hold stored
        under a stale key would otherwise linger un-releasable — no switch maps to
        it — until its TTL or a manual clear.
        """
        stale = [room for room in self._held if room not in valid]
        if stale:
            for room in stale:
                del self._held[room]
            await self._save()
        return stale

    async def sweep_expired(self, now: datetime) -> list[str]:
        """Release holds whose ``expires_at`` has passed. Returns those rooms."""
        expired = [r for r, rec in self._held.items() if rec.expires_at <= now]
        if expired:
            for room in expired:
                del self._held[room]
            await self._save()
        return expired

    def as_attributes(self) -> dict[str, dict[str, str]]:
        """Per-room ``{mode, expires_at}`` map (+ zone overrides) for the sensor."""
        attrs = {
            room: {"mode": rec.kind, "expires_at": rec.expires_at.isoformat()}
            for room, rec in self._held.items()
        }
        for zone, expires in self._overrides.items():
            attrs[zone] = {"mode": MODE_OVERRIDE, "expires_at": expires.isoformat()}
        for zone in self._motion_off:
            attrs[zone] = {"mode": MODE_MOTION_OFF, "expires_at": ""}
        return attrs
