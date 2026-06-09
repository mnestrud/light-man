"""Setup / unload and entity-platform wiring tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.light_man.const import DOMAIN, SERVICE_RELEASE_HOLD

from .conftest import setup_lightman

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_setup_creates_entities_and_services(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A successful setup loads the platforms and registers the services."""
    entry = await setup_lightman(hass)
    assert entry.state is ConfigEntryState.LOADED

    ent_reg = er.async_get(hass)
    domains = {
        e.domain for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    assert {"sensor", "switch"} <= domains
    assert hass.services.has_service(DOMAIN, SERVICE_RELEASE_HOLD)


async def test_unload_removes_services(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """Unloading the last entry cancels subs and drops the services."""
    entry = await setup_lightman(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hass.services.has_service(DOMAIN, SERVICE_RELEASE_HOLD)


async def test_setup_without_seed_retries(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """No seed config -> ConfigEntryNotReady (entry goes to retry)."""
    entry = await setup_lightman(hass, seed=None)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_with_invalid_config_retries(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A structurally invalid seed -> ConfigEntryNotReady (retry)."""
    entry = await setup_lightman(hass, seed={"push_interval_s": 30})
    assert entry.state is ConfigEntryState.SETUP_RETRY
