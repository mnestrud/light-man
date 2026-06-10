"""Tests for the sensor and switch platforms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import mock_restore_cache

from custom_components.light_man.const import (
    DOMAIN,
    UID_ACTIVE_HOLDS,
    UID_PUSH_ENABLE,
    UID_SLEEP_ENABLE,
)

from .conftest import setup_lightman

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _entity_id(hass: HomeAssistant, platform: str, entry_id: str, kind: str) -> str:
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(platform, DOMAIN, f"{entry_id}_{kind}")
    assert entity_id is not None
    return entity_id


async def test_active_holds_sensor_tracks_holds(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """The active-holds sensor reflects the count and per-room expiry."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    sensor_id = _entity_id(hass, "sensor", entry.entry_id, UID_ACTIVE_HOLDS)

    assert hass.states.get(sensor_id).state == "0"

    await coordinator.async_hold("living_room", "night")
    await hass.async_block_till_done()
    state = hass.states.get(sensor_id)
    assert state.state == "1"
    assert state.attributes["rooms"]["living_room"]["mode"] == "night"


async def test_push_enable_switch_toggles_stack(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """The switch defaults ON, and toggling drives the coordinator + booleans."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    switch_id = _entity_id(hass, "switch", entry.entry_id, UID_PUSH_ENABLE)

    # Defaults ON on startup (Light Man is the house default).
    assert coordinator.push_enabled is True
    assert hass.states.get(switch_id).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.push_enabled is False
    assert hass.states.get(switch_id).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.push_enabled is True
    assert hass.states.get(switch_id).state == "on"


async def test_sleep_switch_toggles(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """Toggling the sleep switch drives the coordinator's sleep state."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    switch_id = _entity_id(hass, "switch", entry.entry_id, UID_SLEEP_ENABLE)

    assert hass.states.get(switch_id).state == "off"
    assert coordinator.sleep_on is False

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.sleep_on is True
    assert hass.states.get(switch_id).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.sleep_on is False


async def test_sleep_switch_restores_on(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """A restored 'on' sleep state is re-applied (snapped) on startup."""
    mock_restore_cache(hass, (State("switch.light_man_sleep", "on"),))
    entry = await setup_lightman(hass)
    assert entry.runtime_data.coordinator.sleep_on is True
