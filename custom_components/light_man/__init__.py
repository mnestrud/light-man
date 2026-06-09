"""The Light Man integration.

Phase-0 scaffold. Phase 1 adds the coordinator (timer-driven push + hold manager
+ write-on-change dedup), the MQTT subscriptions that arm/release holds (Inovelli
action topics) and detect room off->on (switch state topics), and the
sensor/switch platforms. Held rooms are excluded by addressing (consolidated vs.
per-room groupcasts), not by mutating Zigbee group membership.
See docs/reference/ARCHITECTURE.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN  # noqa: F401

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Light Man from a config entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
