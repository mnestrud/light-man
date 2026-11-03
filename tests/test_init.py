"""Setup / unload, self-seed, and entity-platform wiring tests."""

from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.light_man import _read_bundled_seed
from custom_components.light_man.config_loader import validate_config
from custom_components.light_man.const import (
    BUNDLED_SEED_VERSION,
    CONFIG_STORE_KEY,
    DOMAIN,
    SERVICE_RELEASE_HOLD,
)

from .conftest import SEED, FakeStore, seed_states, setup_lightman

if TYPE_CHECKING:
    import pytest
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
    entry = await setup_lightman(
        hass, seed={"seed_version": BUNDLED_SEED_VERSION, "push_interval_s": 30}
    )
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def _setup_with_store(
    hass: HomeAssistant, stored: Any, bundle: Any = None
) -> tuple[MockConfigEntry, dict[str, FakeStore]]:
    """Set up with a given stored config (and optional bundle); return the stores."""
    await seed_states(hass)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    stores: dict[str, FakeStore] = {}

    def _factory(_hass: Any, _ver: int, key: str, **_kw: Any) -> FakeStore:
        store = FakeStore(copy.deepcopy(stored) if key == CONFIG_STORE_KEY else None)
        stores[key] = store
        return store

    with (
        patch("custom_components.light_man.Store", side_effect=_factory),
        patch(
            "custom_components.light_man._read_bundled_seed",
            return_value=copy.deepcopy(SEED if bundle is None else bundle),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, stores


async def test_setup_reseeds_when_stored_seed_is_unmigratable(
    hass: HomeAssistant, mqtt_mock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A Store too old to migrate is backed up, then re-seeded from the bundle."""
    old = copy.deepcopy(SEED)
    old.pop("seed_version", None)  # pre-v8: cannot be migrated
    entry, stores = await _setup_with_store(hass, old)
    assert entry.state is ConfigEntryState.LOADED
    assert stores[CONFIG_STORE_KEY]._data == SEED
    assert stores[f"{CONFIG_STORE_KEY}.bak-v0"]._data == old
    assert "re-seeding config from the bundle" in caplog.text


async def test_setup_migrates_v8_store_in_place(
    hass: HomeAssistant, mqtt_mock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A v8 Store is backed up and migrated in place; an edit in it survives."""
    old = copy.deepcopy(SEED)
    old["seed_version"] = 8
    old["curves"]["standard"]["max_br"] = 77
    caplog.set_level(logging.INFO, logger="custom_components.light_man")
    entry, stores = await _setup_with_store(hass, old)
    assert entry.state is ConfigEntryState.LOADED
    saved = stores[CONFIG_STORE_KEY]._data
    assert saved["seed_version"] == BUNDLED_SEED_VERSION
    assert saved["curves"]["standard"]["max_br"] == 77
    assert saved["curves"]["standard"]["day_window"]["wind_down_s"] == 5400.0
    assert stores[f"{CONFIG_STORE_KEY}.bak-v8"]._data == old
    assert "migrated stored config seed_version 8 -> " in caplog.text


async def test_setup_keeps_newer_store_on_downgrade(
    hass: HomeAssistant, mqtt_mock: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A Store from a newer release is kept as is (rollback path) with a warning."""
    newer = copy.deepcopy(SEED)
    newer["seed_version"] = BUNDLED_SEED_VERSION + 1
    entry, stores = await _setup_with_store(hass, newer)
    assert entry.state is ConfigEntryState.LOADED
    assert stores[CONFIG_STORE_KEY]._data == newer
    assert "newer than this release" in caplog.text


async def test_setup_seeds_when_store_empty(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """First run: nothing stored -> the bundle is saved as the Store."""
    entry, stores = await _setup_with_store(hass, None)
    assert entry.state is ConfigEntryState.LOADED
    assert stores[CONFIG_STORE_KEY]._data == SEED


async def test_setup_retries_when_bundle_not_a_mapping(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A corrupt bundle (not a mapping) -> retry rather than saving garbage."""
    entry, _stores = await _setup_with_store(hass, None, bundle=["not", "a", "dict"])
    assert entry.state is ConfigEntryState.SETUP_RETRY


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
    # under_vanity is addressable but not holdable (no switch governs it).
    accent_rooms = result.config["sources"]["accent"]["rooms"]
    assert accent_rooms["primary_bath.under_vanity"]["switches"] == []
