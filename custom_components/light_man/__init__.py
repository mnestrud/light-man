"""The Light Man integration.

Light Man owns the per-source adaptive push (formerly tick areas a16-a19) and
excludes held rooms by *addressing* (consolidated vs. per-room groupcasts), not
by mutating Zigbee group membership. The coordinator drives a timer push + hold
manager + write-on-change dedup; MQTT subscriptions arm/release holds (Inovelli
action topics) and detect room off->on (switch state topics). Topology is loaded
from a Store-seeded JSON. See docs/PLAN.md and docs/reference/ARCHITECTURE.md.
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
    CONFIG_STORE_KEY,
    CONFIG_STORE_VERSION,
    DOMAIN,
    HOLDS_STORE_KEY,
    HOLDS_STORE_VERSION,
    PLATFORMS,
)
from .coordinator import LightManCoordinator
from .holds import HoldManager
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
    if raw is None:
        # First run: self-seed the Store from the bundled default.
        try:
            raw = await hass.async_add_executor_job(_read_bundled_seed)
        except (OSError, ValueError) as err:
            raise ConfigEntryNotReady(f"Light Man seed unreadable: {err}") from err
        await config_store.async_save(raw)
    try:
        validated = validate_config(raw)
    except ValueError as err:
        raise ConfigEntryNotReady(f"Invalid Light Man config: {err}") from err

    holds_store: Store[Any] = Store(hass, HOLDS_STORE_VERSION, HOLDS_STORE_KEY)
    holds = HoldManager(holds_store)
    await holds.async_load()

    coordinator = LightManCoordinator(hass, entry, validated, holds)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = LightManData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Unload a config entry: cancel subs, drop services on the last entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry.runtime_data.coordinator.shutdown_subscriptions()
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            async_unregister_services(hass)
    return unload_ok
