"""Tests for the Light Man services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.light_man.const import (
    DOMAIN,
    SERVICE_CLEAR_HOLDS,
    SERVICE_FORCE_PUSH,
    SERVICE_RELEASE_HOLD,
)
from custom_components.light_man.services import (
    _get_coordinator,
    async_register_services,
)

from .conftest import setup_lightman

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def test_release_hold_unknown_room_raises(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """An unknown room is a validation error."""
    await setup_lightman(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, SERVICE_RELEASE_HOLD, {"room": "nope"}, blocking=True
        )


async def test_release_hold_known_room(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """A known, held room is released by the service."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_arm_hold("living_room")
    await hass.services.async_call(
        DOMAIN, SERVICE_RELEASE_HOLD, {"room": "living_room"}, blocking=True
    )
    assert coordinator.data["held_count"] == 0


async def test_clear_holds_service(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """clear_holds releases everything."""
    entry = await setup_lightman(hass)
    coordinator = entry.runtime_data.coordinator
    await coordinator.async_arm_hold("living_room")
    await coordinator.async_arm_hold("kitchen")
    await hass.services.async_call(DOMAIN, SERVICE_CLEAR_HOLDS, {}, blocking=True)
    assert coordinator.data["held_count"] == 0


async def test_force_push_service(hass: HomeAssistant, mqtt_mock: Any) -> None:
    """force_push runs without error."""
    entry = await setup_lightman(hass)
    await entry.runtime_data.coordinator.async_set_push_enabled(enabled=True)
    await hass.services.async_call(DOMAIN, SERVICE_FORCE_PUSH, {}, blocking=True)


async def test_get_coordinator_not_loaded_raises(hass: HomeAssistant) -> None:
    """Resolving the coordinator with no loaded entry is a validation error."""
    with pytest.raises(ServiceValidationError):
        _get_coordinator(hass)


async def test_register_services_is_idempotent(
    hass: HomeAssistant, mqtt_mock: Any
) -> None:
    """A second registration is a no-op (services already present)."""
    await setup_lightman(hass)
    async_register_services(hass)  # second call returns early
    assert hass.services.has_service(DOMAIN, SERVICE_RELEASE_HOLD)
