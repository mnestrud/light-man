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
from .modes import ModeManager
from .services import async_register_services, async_unregister_services

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class LightManData:
    """Typed ``ConfigEntry.runtime_data`` for Light Man."""

    coordinator: LightManCoordinator


type LightManConfigEntry = ConfigEntry[LightManData]

# Bundled default topology, deployed with the integration (git is source of truth).
# Seeds the Store on first run; Phase 2's OptionsFlow can override it later.
BUNDLED_SEED = "light_man_config.json"


def _read_bundled_seed() -> Any:
    """Read the bundled seed JSON shipped alongside the integration."""
    return json.loads((Path(__file__).parent / BUNDLED_SEED).read_text("utf-8"))


async def async_setup_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Set up Light Man from a config entry."""
    config_store: Store[Any] = Store(hass, CONFIG_STORE_VERSION, CONFIG_STORE_KEY)
    raw = await config_store.async_load()
    if (
        raw is None
        or not isinstance(raw, dict)
        or raw.get("seed_version", 0) < BUNDLED_SEED_VERSION
    ):
        # (Re-)seed the Store from the bundled default: first run, a corrupt
        # store, or a store predating the shipped seed_version (e.g. Phase 2
        # added per-source profiles). Overwriting is safe while the config is
        # code-owned; once the OptionsFlow lets users edit it, merge instead.
        try:
            raw = await hass.async_add_executor_job(_read_bundled_seed)
        except (OSError, ValueError) as err:
            raise ConfigEntryNotReady(f"Light Man seed unreadable: {err}") from err
        await config_store.async_save(raw)
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Unload a config entry: cancel subs, drop services on the last entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data.coordinator
        coordinator.shutdown_subscriptions()
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_services(hass)
    return unload_ok
