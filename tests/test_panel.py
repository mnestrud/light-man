"""Tests for the sidebar panel registration (read-only Phase 2)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from custom_components.light_man import panel as panel_mod

from .conftest import setup_lightman

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_panel_skipped_without_frontend(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """No frontend -> the panel no-ops and the entry still loads (panel optional)."""
    entry = await setup_lightman(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert not hass.data.get(panel_mod._PANEL_REGISTERED)


async def test_panel_registers_and_unregisters(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """With the frontend present, setup registers the panel; unload removes it."""
    await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    with (
        patch.object(
            panel_mod.panel_custom, "async_register_panel", new=AsyncMock()
        ) as reg,
        patch.object(panel_mod.frontend, "async_remove_panel") as rem,
    ):
        entry = await setup_lightman(hass)
        assert reg.await_count == 1
        assert reg.await_args.kwargs["frontend_url_path"] == "light-man"
        assert hass.data[panel_mod._PANEL_REGISTERED] is True

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert rem.called
        assert hass.data[panel_mod._PANEL_REGISTERED] is False


async def test_static_path_registered_once(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """The SPA bundle's static path is registered once, not per (re)register call."""
    await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")
    with (
        patch.object(panel_mod.panel_custom, "async_register_panel", new=AsyncMock()),
        patch.object(panel_mod.frontend, "async_remove_panel"),
        patch.object(hass.http, "async_register_static_paths", new=AsyncMock()) as stat,
    ):
        await setup_lightman(hass)
        await panel_mod.async_register_panel(hass)  # second call is a no-op
        assert stat.await_count == 1
