"""The Light Man integration.

Light Man is the single adaptive brain for the Zigbee bulbs: it owns the
per-source push, the per-room mode (adaptive / held look / off-respect), and a
``push_enable`` master on/off toggle. Modes are driven by explicit Inovelli
action intents over MQTT. Topology + per-source engine profiles load from a
Store-seeded JSON (bundled default on first run).
See docs/PLAN.md and docs/reference/ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .config_loader import validate_config
from .const import (
    BUNDLED_SEED_VERSION,
    CONFIG_STORE_KEY,
    CONFIG_STORE_VERSION,
    DOMAIN,
    MODES_STORE_KEY,
    MODES_STORE_VERSION,
    PLATFORMS,
)
from .coordinator import LightManCoordinator
from .migrations import MigrationError, migrate, stored_version
from .modes import ModeManager
from .panel import async_register_panel, async_unregister_panel
from .services import async_register_services, async_unregister_services
from .websocket import async_register_ws

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class LightManData:
    """Typed ``ConfigEntry.runtime_data`` for Light Man."""

    coordinator: LightManCoordinator


type LightManConfigEntry = ConfigEntry[LightManData]

# Bundled default topology, deployed with the integration (git is source of truth).
# Seeds the Store on first run; later bumps migrate the Store in place.
BUNDLED_SEED = "light_man_config.json"

_LOGGER = logging.getLogger(__name__)


def _read_bundled_seed() -> Any:
    """Read the bundled seed JSON shipped alongside the integration."""
    return json.loads((Path(__file__).parent / BUNDLED_SEED).read_text("utf-8"))


async def async_load_config(hass: HomeAssistant) -> dict[str, Any]:
    """Load the stored config, seeding or migrating it in place as needed.

    - nothing stored, or not a mapping: seed from the bundled default;
    - older ``seed_version``: back up the Store to ``<key>.bak-v<old>``, then
      migrate in place (edits survive); if it cannot be migrated, back up and
      re-seed from the bundle;
    - newer ``seed_version`` (a downgrade): keep it as is and warn.
    """
    store: Store[Any] = Store(hass, CONFIG_STORE_VERSION, CONFIG_STORE_KEY)
    raw = await store.async_load()
    if not isinstance(raw, dict):
        seed = await _bundled_seed(hass)
        await store.async_save(seed)
        return seed
    version = stored_version(raw)
    if version == BUNDLED_SEED_VERSION:
        return raw
    if version > BUNDLED_SEED_VERSION:
        _LOGGER.warning(
            "stored config is seed_version %s, newer than this release's %s; "
            "keeping it (restore %s.bak-v%s to roll back fully)",
            version,
            BUNDLED_SEED_VERSION,
            CONFIG_STORE_KEY,
            BUNDLED_SEED_VERSION,
        )
        return raw
    backup: Store[Any] = Store(
        hass, CONFIG_STORE_VERSION, f"{CONFIG_STORE_KEY}.bak-v{version}"
    )
    await backup.async_save(raw)
    try:
        migrated, steps = migrate(raw)
    except MigrationError as err:
        _LOGGER.warning("re-seeding config from the bundle: %s", err)
        migrated = await _bundled_seed(hass)
    else:
        _LOGGER.info(
            "migrated stored config seed_version %s -> %s (steps %s)",
            version,
            BUNDLED_SEED_VERSION,
            steps,
        )
    await store.async_save(migrated)
    return migrated


async def _bundled_seed(hass: HomeAssistant) -> dict[str, Any]:
    try:
        seed = await hass.async_add_executor_job(_read_bundled_seed)
    except (OSError, ValueError) as err:
        raise ConfigEntryNotReady(f"Light Man seed unreadable: {err}") from err
    if not isinstance(seed, dict):
        raise ConfigEntryNotReady("Light Man seed is not a mapping")
    return seed


async def async_setup_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Set up Light Man from a config entry."""
    raw = await async_load_config(hass)
    try:
        validated = validate_config(raw)
    except ValueError as err:
        raise ConfigEntryNotReady(f"Invalid Light Man config: {err}") from err

    modes_store: Store[Any] = Store(hass, MODES_STORE_VERSION, MODES_STORE_KEY)
    modes = ModeManager(modes_store)
    await modes.async_load()

    coordinator = LightManCoordinator(hass, entry, validated, modes)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = LightManData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    async_register_ws(hass)
    await async_register_panel(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Unload a config entry: cancel subs, drop services on the last entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data.coordinator
        coordinator.shutdown_subscriptions()
        async_unregister_panel(hass)
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_services(hass)
    return unload_ok
