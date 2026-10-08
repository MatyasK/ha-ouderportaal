"""Base entity: one device per child."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OuderportaalCoordinator
from .timeline import Moment, Photo


class OuderportaalEntity(CoordinatorEntity[OuderportaalCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: OuderportaalCoordinator, child_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.child_id = child_id
        self._attr_unique_id = f"{child_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, child_id)},
            name=coordinator.child_name(child_id),
            manufacturer="Konnect",
            model="Ouderportaal",
            configuration_url=f"{coordinator.client.base_url}/parent/",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def moments(self) -> list[Moment]:
        """This child's moments, oldest first."""
        return [m for m in self.coordinator.data.moments if m.child_id == self.child_id]

    @property
    def photos(self) -> list[Photo]:
        """This child's photos, newest first."""
        return [p for p in self.coordinator.data.photos if self.child_id in p.child_ids]
