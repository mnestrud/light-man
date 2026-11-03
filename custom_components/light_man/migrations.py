"""In-place migration of the stored config across bundled ``seed_version`` bumps.

The Store (``.storage/light_man_config``) is the live authority. When a release
ships a newer bundled seed, the stored config is **migrated in place** rather than
overwritten, so anything edited in the Store survives. Each step is a pure
function ``vN -> vN+1`` on a deep copy; the loader backs up the old Store first.

Versions:
  v8 -> v9  make implicit curve defaults explicit (``sat``, day-window
            ``edge_transition_s`` / ``wind_down_s``). Engine output is identical.
  v9 -> v10 ``push_interval_s`` 30 -> 120 (bulb-side glide makes the longer
            interval invisible; a value other than the old default is kept).
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from .const import (
    BUNDLED_SEED_VERSION,
    CONF_CURVES,
    DEFAULT_EDGE_TRANSITION_S,
    DEFAULT_SAT,
    DEFAULT_WIND_DOWN_S,
)

if TYPE_CHECKING:
    from collections.abc import Callable

SEED_VERSION_KEY = "seed_version"
MIN_MIGRATABLE_VERSION = 8


class MigrationError(ValueError):
    """The stored config cannot be migrated to the bundled version."""


def _v8_to_v9(raw: dict[str, Any]) -> dict[str, Any]:
    for curve in (raw.get(CONF_CURVES) or {}).values():
        if not isinstance(curve, dict):
            continue
        curve.setdefault("sat", DEFAULT_SAT)
        window = curve.get("day_window")
        if isinstance(window, dict):
            window.setdefault("edge_transition_s", DEFAULT_EDGE_TRANSITION_S)
            window.setdefault("wind_down_s", DEFAULT_WIND_DOWN_S)
    return raw


V9_PUSH_INTERVAL_S = 30
V10_PUSH_INTERVAL_S = 120


def _v9_to_v10(raw: dict[str, Any]) -> dict[str, Any]:
    if raw.get("push_interval_s") == V9_PUSH_INTERVAL_S:
        raw["push_interval_s"] = V10_PUSH_INTERVAL_S
    return raw


MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {
    8: _v8_to_v9,
    9: _v9_to_v10,
}


def stored_version(raw: dict[str, Any]) -> int:
    """Return the stored seed version (0 if absent or not an int)."""
    version = raw.get(SEED_VERSION_KEY, 0)
    return version if isinstance(version, int) else 0


def migrate(
    raw: dict[str, Any], *, target: int = BUNDLED_SEED_VERSION
) -> tuple[dict[str, Any], list[int]]:
    """Return ``(migrated copy, versions stepped through)``; raise MigrationError."""
    version = stored_version(raw)
    if version < MIN_MIGRATABLE_VERSION:
        msg = f"stored seed_version {version} predates v{MIN_MIGRATABLE_VERSION}"
        raise MigrationError(msg)
    migrated = copy.deepcopy(raw)
    steps: list[int] = []
    while version < target:
        step = MIGRATIONS.get(version)
        if step is None:
            msg = f"no migration from seed_version {version}"
            raise MigrationError(msg)
        migrated = step(migrated)
        steps.append(version)
        version += 1
        migrated[SEED_VERSION_KEY] = version
    return migrated, steps
