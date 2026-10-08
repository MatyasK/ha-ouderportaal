"""Latest daycare photo per child."""

from __future__ import annotations

import mimetypes
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import OuderportaalConfigEntry, OuderportaalCoordinator
from .entity import OuderportaalEntity
from .timeline import Photo


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OuderportaalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OuderportaalLatestPhoto(hass, coordinator, child_id) for child_id in coordinator.children)


class OuderportaalLatestPhoto(OuderportaalEntity, ImageEntity):
    """The newest photo, served from the saved copy in the media folder."""

    def __init__(self, hass: HomeAssistant, coordinator: OuderportaalCoordinator, child_id: str) -> None:
        OuderportaalEntity.__init__(self, coordinator, child_id, "latest_photo")
        ImageEntity.__init__(self, hass)
        self._photo: Photo | None = None
        self._update_photo()

    def _update_photo(self) -> None:
        photo = next((p for p in self.photos if p.media_type == "photo"), None)
        if photo and (self._photo is None or photo.id != self._photo.id):
            self._photo = photo
            self._attr_image_last_updated = dt_util.now()
            self._attr_content_type = mimetypes.guess_type(photo.filename)[0] or "image/jpeg"

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_photo()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        if self._photo is None:
            return None
        path = self.coordinator.archive.path(self._photo)
        return await self.hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)

    @property
    def available(self) -> bool:
        return super().available and self._photo is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._photo is None:
            return {}
        return {
            "photo_id": self._photo.id,
            "date": self._photo.day.isoformat(),
            "description": self._photo.description,
            "media_url": self.coordinator.archive.media_url(self._photo),
        }
