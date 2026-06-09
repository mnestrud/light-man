"""Sensor platform: one active-holds diagnostic sensor."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory

from .const import UID_ACTIVE_HOLDS
from .entity import LightManEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import LightManConfigEntry
    from .coordinator import LightManCoordinator

# Push-driven; HA never polls these entities.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightManConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the active-holds sensor."""
    async_add_entities([ActiveHoldsSensor(entry.runtime_data.coordinator)])


class ActiveHoldsSensor(LightManEntity, SensorEntity):
    """Count of currently held rooms, with each room's expiry as an attribute."""

    _attr_translation_key = UID_ACTIVE_HOLDS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LightManCoordinator) -> None:
        """Bind to the coordinator as the active-holds singleton."""
        super().__init__(coordinator, UID_ACTIVE_HOLDS)

    @property
    def native_value(self) -> int:
        """Number of rooms currently holding a manual scene."""
        return self.coordinator.data["held_count"]

    @property
    def extra_state_attributes(self) -> dict[str, dict[str, str]]:
        """Per-room ``expires_at`` map."""
        return {"rooms": self.coordinator.data["held"]}
