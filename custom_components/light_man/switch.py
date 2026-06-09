"""Switch platform: the single-toggle stack switch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .const import UID_PUSH_ENABLE
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
    """Set up the push-enable switch."""
    async_add_entities([PushEnableSwitch(entry.runtime_data.coordinator)])


class PushEnableSwitch(LightManEntity, SwitchEntity):
    """ON: Light Man owns the push (legacy floods off). OFF: revert to legacy."""

    _attr_translation_key = UID_PUSH_ENABLE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: LightManCoordinator) -> None:
        """Bind to the coordinator as the push-enable singleton."""
        super().__init__(coordinator, UID_PUSH_ENABLE)

    @property
    def is_on(self) -> bool:
        """True when Light Man owns the adaptive push."""
        return self.coordinator.push_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Take over the push and turn the legacy floods off."""
        await self.coordinator.async_set_push_enabled(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hand the push back to the legacy stack."""
        await self.coordinator.async_set_push_enabled(enabled=False)
