"""Light Man services: release_hold, clear_holds, force_push."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ROOM,
    DOMAIN,
    SERVICE_CLEAR_HOLDS,
    SERVICE_FORCE_PUSH,
    SERVICE_RELEASE_HOLD,
)

if TYPE_CHECKING:
    from .coordinator import LightManCoordinator

RELEASE_HOLD_SCHEMA = vol.Schema({vol.Required(ATTR_ROOM): cv.string})


def _get_coordinator(hass: HomeAssistant) -> LightManCoordinator:
    """Return the single loaded coordinator, or raise if not set up."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = getattr(entry, "runtime_data", None)
        if data is not None:
            coordinator: LightManCoordinator = data.coordinator
            return coordinator
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="not_loaded"
    )


async def _async_release_hold(call: ServiceCall) -> None:
    """Release a single room's hold (bad room -> ServiceValidationError)."""
    coordinator = _get_coordinator(call.hass)
    room = call.data[ATTR_ROOM]
    if not coordinator.is_known_room(room):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_room",
            translation_placeholders={"room": room},
        )
    await coordinator.async_release_hold(room)


async def _async_clear_holds(call: ServiceCall) -> None:
    """Release every hold."""
    await _get_coordinator(call.hass).async_clear_holds()


async def _async_force_push(call: ServiceCall) -> None:
    """Re-publish every source now, bypassing dedup."""
    await _get_coordinator(call.hass).async_force_push()


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register Light Man's services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_RELEASE_HOLD):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_HOLD, _async_release_hold, schema=RELEASE_HOLD_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_HOLDS, _async_clear_holds)
    hass.services.async_register(DOMAIN, SERVICE_FORCE_PUSH, _async_force_push)


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove Light Man's services (on the last entry unload)."""
    for service in (SERVICE_RELEASE_HOLD, SERVICE_CLEAR_HOLDS, SERVICE_FORCE_PUSH):
        hass.services.async_remove(DOMAIN, service)
