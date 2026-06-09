"""Tests for the sensor and switch platforms."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er

from custom_components.light_man.const import (
    DOMAIN,
    UID_ACTIVE_HOLDS,
    UID_PUSH_ENABLE,
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
    """Toggling the switch drives the coordinator + legacy booleans."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    switch_id = _entity_id(hass, "switch", entry.entry_id, UID_PUSH_ENABLE)

    assert hass.states.get(switch_id).state == "off"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.push_enabled is True
    assert hass.states.get(switch_id).state == "on"

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    assert coordinator.push_enabled is False
    assert hass.states.get(switch_id).state == "off"
