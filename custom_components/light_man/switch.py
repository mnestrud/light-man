"""Switch platform: the stack toggle and the sleep toggle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import CoreState
from homeassistant.helpers.restore_state import RestoreEntity

from .const import UID_PUSH_ENABLE, UID_SLEEP_ENABLE
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
    """Set up the push-enable and sleep switches."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([PushEnableSwitch(coordinator), SleepSwitch(coordinator)])


class PushEnableSwitch(LightManEntity, RestoreEntity, SwitchEntity):
    """Master on/off. ON: Light Man runs the adaptive push. OFF: Light Man is inert.

    Defaults ON and restores across restarts, so Light Man is the house default
    and you never have to re-enable it after a reboot.
    """

    _attr_translation_key = UID_PUSH_ENABLE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: LightManCoordinator) -> None:
        """Bind to the coordinator as the push-enable singleton."""
        super().__init__(coordinator, UID_PUSH_ENABLE)

    async def async_added_to_hass(self) -> None:
        """Restore the enabled state on startup unless it was turned off.

        On a cold boot we only set the desired state here; the coordinator's
        ``async_at_started`` hook runs the first push once HA is fully up. When
        added while HA is already running (a reload), push now.
        """
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        enabled = last is None or last.state == STATE_ON
        self.coordinator.push_enabled = enabled
        if enabled and self.hass.state is CoreState.running:
            await self.coordinator.async_set_push_enabled(enabled=True)

    @property
    def is_on(self) -> bool:
        """True when Light Man is running the adaptive push."""
        return self.coordinator.push_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Light Man's adaptive push."""
        await self.coordinator.async_set_push_enabled(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Light Man — it goes inert (no pushes)."""
        await self.coordinator.async_set_push_enabled(enabled=False)


class SleepSwitch(LightManEntity, RestoreEntity, SwitchEntity):
    """Global sleep toggle: ON ramps the house into its per-source sleep target.

    The user owns the schedule (automate this switch at bedtime); Light Man owns
    the ramp. State is restored across restarts so a bedtime hold is not dropped.
    """

    _attr_translation_key = UID_SLEEP_ENABLE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: LightManCoordinator) -> None:
        """Bind to the coordinator as the sleep singleton."""
        super().__init__(coordinator, UID_SLEEP_ENABLE)

    async def async_added_to_hass(self) -> None:
        """Restore the last sleep state, snapped (no ramp) on startup."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            await self.coordinator.async_set_sleep(enabled=True, ramp=False)

    @property
    def is_on(self) -> bool:
        """True when the sleep overlay is engaged."""
        return self.coordinator.sleep_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Ramp the house into the sleep target."""
        await self.coordinator.async_set_sleep(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Ramp the house back out of the sleep target."""
        await self.coordinator.async_set_sleep(enabled=False)
