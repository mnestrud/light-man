"""Tests for the diagnostics dump."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.light_man.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import setup_lightman

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_diagnostics_shape(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """The diagnostics dump exposes push-health, holds, and the config shape."""
    entry = await setup_lightman(hass)
    await entry.runtime_data.coordinator.async_hold("kitchen.overhead", "night")

    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["push_enabled"] is True  # Light Man is the default on startup
    assert diag["mqtt_available"] is True
    assert "kitchen.overhead" in diag["held"]
    assert diag["config"]["push_interval_s"] == 30
    assert set(diag["config"]["sources"]) == {"overhead", "hallway_up"}
    assert "dedup_skips" in diag["push_health"]
