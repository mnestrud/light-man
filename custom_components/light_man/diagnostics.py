"""Diagnostics for Light Man (push-health + config shape, no secrets)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import LightManConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LightManConfigEntry
) -> dict[str, Any]:
    """Return push-health, hold state, surfaced config issues, and config shape."""
    coordinator = entry.runtime_data.coordinator
    return {
        "push_enabled": coordinator.push_enabled,
        "mqtt_available": coordinator.mqtt_available,
        "held": coordinator.data["held"],
        "push_health": coordinator.diagnostics,
        "config": coordinator.config_summary(),
    }
