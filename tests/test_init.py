"""Setup / unload, self-seed, and entity-platform wiring tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_man import _read_bundled_seed
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import (
    CONFIG_STORE_KEY,
    DOMAIN,
    SERVICE_RELEASE_HOLD,
)

from .conftest import SEED, FakeStore, seed_states, setup_lightman

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


async def test_setup_self_seeds_from_bundled(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """An empty Store self-seeds from the bundled default and loads."""
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, title="Light Man", unique_id=DOMAIN)
    entry.add_to_hass(hass)

    def _factory(*_a: Any, **_kw: Any) -> FakeStore:
        return FakeStore(None)  # Store starts empty -> triggers self-seed

    with (
        patch("custom_components.light_man.Store", side_effect=_factory),
        patch(
            "custom_components.light_man._read_bundled_seed",
            return_value=copy.deepcopy(SEED),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_setup_bundled_unreadable_retries(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """Empty Store + unreadable bundled seed -> ConfigEntryNotReady (retry)."""
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    def _factory(*_a: Any, **_kw: Any) -> FakeStore:
        return FakeStore(None)

    with (
        patch("custom_components.light_man.Store", side_effect=_factory),
        patch(
            "custom_components.light_man._read_bundled_seed",
            side_effect=FileNotFoundError("missing"),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_with_invalid_config_retries(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A current-version but structurally invalid Store -> retry (not re-seeded)."""
    entry = await setup_lightman(hass, seed={"seed_version": 6, "push_interval_s": 30})
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_reseeds_when_stored_seed_is_old(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A Store predating the bundled seed_version is re-seeded from the bundle."""
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    old = copy.deepcopy(SEED)
    old.pop("seed_version", None)  # simulate a pre-profile (pre-v2) stored config
    stores: dict[str, FakeStore] = {}

    def _factory(_hass: Any, _ver: int, key: str, **_kw: Any) -> FakeStore:
        store = FakeStore(copy.deepcopy(old) if key == CONFIG_STORE_KEY else None)
        stores[key] = store
        return store

    with (
        patch("custom_components.light_man.Store", side_effect=_factory),
        patch(
            "custom_components.light_man._read_bundled_seed",
            return_value=copy.deepcopy(SEED),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert stores[CONFIG_STORE_KEY]._data.get("seed_version") == 6


def test_bundled_seed_is_valid() -> None:
    """The shipped seed must always validate cleanly with no soft issues."""
    result = validate_config(_read_bundled_seed())
    assert result.issues == []
    assert set(result.config["sources"]) == {
        "overhead",
        "accent",
        "hallway_up",
        "hallway_down",
    }
    # under_vanity is addressable but not holdable (no switch).
    assert result.config["sources"]["accent"]["rooms"]["under_vanity"]["switches"] == []
