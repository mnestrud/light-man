"""The Light Man integration.

Light Man owns the per-source adaptive push (formerly tick areas a16-a19) and
excludes held rooms by *addressing* (consolidated vs. per-room groupcasts), not
by mutating Zigbee group membership. The coordinator drives a timer push + hold
manager + write-on-change dedup; MQTT subscriptions arm/release holds (Inovelli
action topics) and detect room off->on (switch state topics). Topology is loaded
from a Store-seeded JSON. See docs/PLAN.md and docs/reference/ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
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


async def async_setup_entry(hass: HomeAssistant, entry: LightManConfigEntry) -> bool:
    """Set up Light Man from a config entry."""
    config_store: Store[Any] = Store(hass, CONFIG_STORE_VERSION, CONFIG_STORE_KEY)
    raw = await config_store.async_load()
    if raw is None:
        raise ConfigEntryNotReady(
            f"Light Man config not seeded (expected Store '{CONFIG_STORE_KEY}')"
        )
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
