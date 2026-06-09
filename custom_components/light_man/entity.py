"""Shared base entity for Light Man's house-wide singletons."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import LightManCoordinator


class LightManEntity(CoordinatorEntity["LightManCoordinator"]):
    """Base for the active-holds sensor and the push-enable switch.

    Both are house-wide singletons attached to one service device, so the
    unique_id is ``{entry_id}_{kind}`` (the ``{room|source}`` slot of the locked
    scheme is unused here).
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: LightManCoordinator, kind: str) -> None:
        """Set unique_id and the shared service device."""
        super().__init__(coordinator)
        entry_id = coordinator.entry_id
        self._attr_unique_id = f"{entry_id}_{kind}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Light Man",
            manufacturer="Light Man",
            entry_type=DeviceEntryType.SERVICE,
        )
