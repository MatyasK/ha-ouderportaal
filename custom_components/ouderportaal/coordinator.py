"""Polls the timeline, keeps the history and photos, and fires events for new entries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OuderportaalAuthError, OuderportaalClient, OuderportaalError
from .archive import PhotoArchive
from .const import (
    BACKFILL_DAYS,
    BACKFILL_MAX_PAGES,
    BACKFILL_PAGE_DELAY_S,
    BACKFILL_VERSION,
    DAY_END_HOUR,
    DAY_START_HOUR,
    DAY_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ENTRY,
    NIGHT_UPDATE_INTERVAL,
    STORAGE_VERSION,
)
from .timeline import Moment, Photo, Timeline, merge_timelines, parse_timeline

_LOGGER = logging.getLogger(__name__)

type OuderportaalConfigEntry = ConfigEntry[OuderportaalCoordinator]


@dataclass(frozen=True, slots=True)
class Child:
    id: str
    first_name: str
    full_name: str


def update_interval_for(now: datetime) -> timedelta:
    return DAY_UPDATE_INTERVAL if DAY_START_HOUR <= now.hour < DAY_END_HOUR else NIGHT_UPDATE_INTERVAL


class OuderportaalCoordinator(DataUpdateCoordinator[Timeline]):
    """Polls timeline page 0; the data is the full history collected so far."""

    config_entry: OuderportaalConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OuderportaalConfigEntry, client: OuderportaalClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=update_interval_for(dt_util.now()),
        )
        self.client = client
        self.archive = PhotoArchive(hass)
        self.children: dict[str, Child] = {}
        self._seen_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seen")
        self._history_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.history")
        # None until loaded: an empty store means "first run", which must not notify about history.
        self._seen_moments: dict[str, str] | None = None
        self._seen_photos: set[str] = set()
        self.history = Timeline(moments=[], photos=[])
        self._backfill_version = 0

    async def _async_setup(self) -> None:
        try:
            children = await self.client.get_children()
        except OuderportaalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OuderportaalError as err:
            raise UpdateFailed(str(err)) from err
        self.children = {
            c["id"]: Child(id=c["id"], first_name=c.get("firstName") or c["id"], full_name=c.get("fullname") or "")
            for c in children
        }
        if stored := await self._seen_store.async_load():
            self._seen_moments = stored.get("moments", {})
            self._seen_photos = set(stored.get("photos", []))
        if stored := await self._history_store.async_load():
            self.history = Timeline.from_dict(stored.get("timeline", {}))
            # Stores from before versioning ("backfilled": true) imported nothing: treat them as version 0.
            self._backfill_version = stored.get("backfill_version", 0)

    async def _async_update_data(self) -> Timeline:
        self.update_interval = update_interval_for(dt_util.now())
        try:
            cards = await self.client.get_timeline(0)
        except OuderportaalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OuderportaalError as err:
            raise UpdateFailed(str(err)) from err

        timeline = parse_timeline(cards, dt_util.get_default_time_zone())
        # Save photos before announcing them, so a notification can show the saved copy.
        await self.archive.async_download_missing(timeline.photos)
        # An empty answer is more likely a hiccup than a wiped timeline: keep the seen state.
        if timeline.moments or timeline.photos:
            await self._announce(timeline)
        await self._add_to_history(timeline)
        return self.history

    async def async_backfill(self) -> None:
        """Import the last BACKFILL_DAYS days once, without announcing them. Runs in the background."""
        if self._backfill_version >= BACKFILL_VERSION:
            return
        since = dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
        tz = dt_util.get_default_time_zone()
        offset = 0
        for _ in range(BACKFILL_MAX_PAGES):
            try:
                cards = await self.client.get_timeline(offset)
            except OuderportaalError as err:
                _LOGGER.warning("Importing history stopped at card %s, will retry after a restart: %s", offset, err)
                return
            if not cards:
                break
            # Like the portal's own infinite scroll: the next request starts after the cards we have.
            offset += len(cards)
            fetched = parse_timeline(cards, tz)
            await self.archive.async_download_missing(p for p in fetched.photos if p.day >= since)
            await self._add_to_history(fetched, since=since)
            self.async_set_updated_data(self.history)
            days = [m.day for m in fetched.moments] + [p.day for p in fetched.photos]
            if days and min(days) < since:
                break
            await asyncio.sleep(BACKFILL_PAGE_DELAY_S)
        self._backfill_version = BACKFILL_VERSION
        await self._save_history()
        _LOGGER.info(
            "Imported history since %s: %s moments, %s photos",
            since,
            len(self.history.moments),
            len(self.history.photos),
        )

    async def _add_to_history(self, timeline: Timeline, *, since: date | None = None) -> None:
        merged = merge_timelines(self.history, timeline, since=since)
        if merged.to_dict() != self.history.to_dict():
            self.history = merged
            await self._save_history()

    async def _save_history(self) -> None:
        await self._history_store.async_save(
            {"backfill_version": self._backfill_version, "timeline": self.history.to_dict()}
        )

    async def _announce(self, timeline: Timeline) -> None:
        first_run = self._seen_moments is None
        seen_moments = self._seen_moments or {}

        if not first_run:
            for moment in timeline.moments:
                previous = seen_moments.get(moment.key)
                if previous != moment.fingerprint:
                    self._fire_moment(moment, "new" if previous is None else "updated")
            new_photos: dict[str, list[Photo]] = {}
            for photo in timeline.photos:
                if photo.id not in self._seen_photos:
                    for child_id in photo.child_ids:
                        new_photos.setdefault(child_id, []).append(photo)
            for child_id, photos in new_photos.items():
                self._fire_photos(child_id, photos)

        # Only page 0 is tracked; days that roll off it never come back, so prune to what is visible.
        moments = {m.key: m.fingerprint for m in timeline.moments}
        photos = {p.id for p in timeline.photos}
        if moments == self._seen_moments and photos == self._seen_photos:
            return
        self._seen_moments, self._seen_photos = moments, photos
        await self._seen_store.async_save({"moments": moments, "photos": sorted(photos)})

    def child_name(self, child_id: str) -> str:
        child = self.children.get(child_id)
        return child.first_name if child else child_id

    def _fire_moment(self, moment: Moment, change: str) -> None:
        self.hass.bus.async_fire(
            EVENT_NEW_ENTRY,
            {
                "entry_type": "moment",
                "change": change,
                "child_id": moment.child_id,
                "child_name": self.child_name(moment.child_id),
                "category": moment.category,
                "icon": moment.icon,
                "title": moment.title,
                "text": moment.text,
                "label": moment.label,
                "description": moment.description,
                "remarks": moment.remarks,
                "date": moment.day.isoformat(),
                "start": moment.start.isoformat() if moment.start else None,
                "end": moment.end.isoformat() if moment.end else None,
                "duration_minutes": moment.duration_minutes,
                "amount_ml": moment.amount_ml,
            },
        )

    def _fire_photos(self, child_id: str, photos: list[Photo]) -> None:
        """One event per batch, so an upload of 20 photos is one notification."""
        self.hass.bus.async_fire(
            EVENT_NEW_ENTRY,
            {
                "entry_type": "photos",
                "change": "new",
                "child_id": child_id,
                "child_name": self.child_name(child_id),
                "count": len(photos),
                # The saved copy, served by Home Assistant; the companion app loads it with your login.
                "media_url": self.archive.media_url(photos[0]),
                # The portal's signed URL; works anywhere but expires after about an hour.
                "image_url": photos[0].medium_url or photos[0].full_url,
                "photos": [
                    {
                        "id": p.id,
                        "date": p.day.isoformat(),
                        "description": p.description,
                        "media_type": p.media_type,
                        "media_url": self.archive.media_url(p),
                        "media_content_id": self.archive.media_content_id(p),
                        "full_url": p.full_url,
                        "medium_url": p.medium_url,
                        "thumb_url": p.thumb_url,
                    }
                    for p in photos
                ],
            },
        )
