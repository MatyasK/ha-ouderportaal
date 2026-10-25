"""Polls the timeline, keeps the history and photos, and fires events for new entries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import OuderportaalAuthError, OuderportaalClient, OuderportaalError
from .archive import PhotoArchive
from .const import (
    AUTH_AGENDA,
    AUTH_MESSAGES,
    AUTH_NEWS,
    AUTH_NEWSLETTERS,
    BACKFILL_DAYS,
    BACKFILL_MAX_PAGES,
    BACKFILL_PAGE_DELAY_S,
    BACKFILL_VERSION,
    DAY_END_HOUR,
    DAY_START_HOUR,
    DAY_UPDATE_INTERVAL,
    DOMAIN,
    EVENT_NEW_ENTRY,
    HISTORY_SAVE_DELAY_S,
    LATEST_PHOTO_KEY,
    MESSAGES_POLL_DAYS,
    NIGHT_UPDATE_INTERVAL,
    SCHEDULE_FUTURE_DAYS,
    SCHEDULE_PAST_DAYS,
    SCHEDULE_REQUEST_DAYS,
    SCHEDULE_UPDATE_INTERVAL,
    STORAGE_VERSION,
)
from .schedule import Session, parse_schedule, schedule_ranges
from .timeline import (
    Message,
    Moment,
    Photo,
    Timeline,
    merge_timelines,
    parse_messages,
    parse_news,
    parse_newsletters,
    parse_timeline,
)

_LOGGER = logging.getLogger(__name__)

type OuderportaalConfigEntry = ConfigEntry[OuderportaalCoordinator]


@dataclass(frozen=True, slots=True)
class Child:
    id: str
    first_name: str
    full_name: str


def child_unique_id(coordinator: OuderportaalCoordinator, child_id: str) -> str:
    """The child in this account: '<entry id>_<child id>'.

    Scoped to the account because installs from before the one-account-per-portal rule may have both parents' accounts.

    The base of the child's device identifier and entity unique ids.
    """
    return f"{coordinator.config_entry.entry_id}_{child_id}"


def child_entity_unique_id(coordinator: OuderportaalCoordinator, child_id: str, key: str) -> str:
    return f"{child_unique_id(coordinator, child_id)}_{key}"


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
        # What the account may see, e.g. 'WEB_HTML_NEWS_VIEW'; read once at setup, like the portal does at login.
        self.authorizations: set[str] = set()
        self._seen_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seen")
        self._history_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.history")
        # A save is waiting for its delay.
        self._history_unsaved = False
        # None until loaded: an empty store means "first run", which must not notify about history.
        self._seen_moments: dict[str, str] | None = None
        self._seen_photos: set[str] = set()
        # Message key -> fingerprint, for the items in the poll window.
        self._seen_messages: dict[str, str] | None = None
        self.history = Timeline(moments=[], photos=[])
        self._backfill_version = 0
        # The portal's own unread counts ('messages', 'news', 'newsletters'); None until read once.
        self.unread_counts: dict[str, int] | None = None
        self._messages_failing = False
        # Set up after the first refresh, when the account may see the Agenda.
        self.schedule: ScheduleCoordinator | None = None

    async def _async_setup(self) -> None:
        try:
            children = await self.client.get_children()
            self.authorizations = await self.client.get_authorizations()
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
            # Missing in stores from before news and newsletters were supported (they kept conversations as
            # 'messages'): then the next poll is a silent baseline.
            self._seen_messages = stored.get("mailbox")
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
        # Save photos and add them to the history before announcing them: a notification shows the latest photo
        # image, which serves the newest saved photo in the history.
        await self.archive.async_download_missing(timeline.photos)
        self._add_to_history(timeline)
        # An empty answer is more likely a hiccup than a wiped timeline: keep the seen state.
        if timeline.moments or timeline.photos:
            await self._announce(timeline)

        today = dt_util.now().date()
        try:
            messages = await self._fetch_messages(today - timedelta(days=MESSAGES_POLL_DAYS), today + timedelta(days=1))
        except OuderportaalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        if messages is not None:
            await self._announce_messages(messages)
            self._add_to_history(Timeline(moments=[], photos=[], messages=messages))
            await self._update_unread()

        return self.history

    async def _fetch_messages(self, start: date, end: date) -> list[Message] | None:
        """Everything on the portal's Messages page, newest first.

        Messages are an extra: when they fail, the rest of the update still goes through. All or nothing, so a
        failing part does not look like its items were removed. A rejected login is raised.
        """
        tz = dt_util.get_default_time_zone()
        messages: list[Message] = []
        try:
            if AUTH_MESSAGES in self.authorizations:
                messages += parse_messages(await self.client.get_messages(start, end), tz)
            if AUTH_NEWS in self.authorizations:
                messages += parse_news(await self.client.get_news(start, end), tz)
            if AUTH_NEWSLETTERS in self.authorizations:
                messages += parse_newsletters(await self.client.get_newsletters(start, end), tz)
        except OuderportaalAuthError:
            raise
        except OuderportaalError as err:
            if not self._messages_failing:
                _LOGGER.warning("Could not read messages, will keep trying: %s", err)
            self._messages_failing = True
            return None
        if self._messages_failing:
            _LOGGER.info("Reading messages works again")
        self._messages_failing = False
        messages.sort(key=lambda m: m.sent, reverse=True)
        return messages

    @property
    def messages_failing(self) -> bool:
        """The Messages page could not be read the last time."""
        return self._messages_failing

    @property
    def history_imported(self) -> bool:
        """The history import has run to the end, for this version of the import."""
        return self._backfill_version >= BACKFILL_VERSION

    @property
    def has_schedule(self) -> bool:
        """Whether the portal shows this account its Agenda."""
        return not AUTH_AGENDA.isdisjoint(self.authorizations)

    async def _update_unread(self) -> None:
        try:
            counts = await self.client.get_notifications()
        except OuderportaalError as err:
            _LOGGER.debug("Could not read the unread count: %s", err)
            return
        # The portal sends null instead of 0.
        self.unread_counts = {
            "messages": counts.get("nrOfNewMessages") or 0,
            "news": counts.get("nrOfNewNewsItems") or 0,
            "newsletters": counts.get("nrOfNewNewsletters") or 0,
        }

    async def async_backfill(self) -> None:
        """Import the last BACKFILL_DAYS days once, without announcing them. Runs in the background."""
        if self.history_imported:
            return
        try:
            await self._backfill()
        except OuderportaalAuthError as err:
            # Outside an update, so nothing else asks to log in again. Logging in reloads the entry, which imports.
            _LOGGER.warning("Importing history postponed until you log in again: %s", err)
            self.config_entry.async_start_reauth(self.hass)

    async def _backfill(self) -> None:
        since = dt_util.now().date() - timedelta(days=BACKFILL_DAYS)
        tz = dt_util.get_default_time_zone()
        messages = await self._fetch_messages(since, dt_util.now().date() + timedelta(days=1))
        if messages is None:
            _LOGGER.warning("Importing history postponed until messages can be read; will retry after a restart")
            return
        self._add_to_history(Timeline(moments=[], photos=[], messages=messages), since=since)
        offset = 0
        for _ in range(BACKFILL_MAX_PAGES):
            try:
                cards = await self.client.get_timeline(offset)
            except OuderportaalAuthError:
                raise
            except OuderportaalError as err:
                _LOGGER.warning("Importing history stopped at card %s, will retry after a restart: %s", offset, err)
                return
            if not cards:
                break
            # Like the portal's own infinite scroll: the next request starts after the cards we have.
            offset += len(cards)
            fetched = parse_timeline(cards, tz)
            await self.archive.async_download_missing(p for p in fetched.photos if p.day >= since)
            self._add_to_history(fetched, since=since)
            self.async_set_updated_data(self.history)
            days = [m.day for m in fetched.moments] + [p.day for p in fetched.photos]
            if days and min(days) < since:
                break
            await asyncio.sleep(BACKFILL_PAGE_DELAY_S)
        self._backfill_version = BACKFILL_VERSION
        self._save_history()
        _LOGGER.info(
            "Imported history since %s: %s moments, %s photos, %s messages",
            since,
            len(self.history.moments),
            len(self.history.photos),
            len(self.history.messages),
        )

    @callback
    def _add_to_history(self, timeline: Timeline, *, since: date | None = None) -> None:
        merged = merge_timelines(self.history, timeline, since=since)
        # Compares the entries, not the photos' signed URLs, which change on every poll and are not stored.
        if merged != self.history:
            self.history = merged
            self._save_history()

    @callback
    def _save_history(self) -> None:
        """Written a little later, so a burst of changes, such as the history import's pages, is written once."""
        self._history_unsaved = True
        self._history_store.async_delay_save(self._history_data, HISTORY_SAVE_DELAY_S)

    def _history_data(self) -> dict[str, Any]:
        return {"backfill_version": self._backfill_version, "timeline": self.history.to_dict()}

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        # Write now what is waiting to be written: the entry can be set up again right away, and reads the file.
        if self._history_unsaved:
            self._history_unsaved = False
            await self._history_store.async_save(self._history_data())

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
        await self._save_seen()

    async def _announce_messages(self, messages: list[Message]) -> None:
        if self._seen_messages is not None:
            known = {m.key for m in self.history.messages}
            for message in reversed(messages):  # oldest first, like the moments
                if self._seen_messages.get(message.key) != message.fingerprint:
                    self._fire_message(message, "updated" if message.key in known else "new")
        # Only the poll window is tracked; a conversation that comes back into it has a new reply.
        seen = {m.key: m.fingerprint for m in messages}
        if seen != self._seen_messages:
            self._seen_messages = seen
            await self._save_seen()

    async def _save_seen(self) -> None:
        data: dict[str, Any] = {"moments": self._seen_moments or {}, "photos": sorted(self._seen_photos)}
        # Left out until known, so a restart before the first successful read stays a silent baseline.
        if self._seen_messages is not None:
            data["mailbox"] = self._seen_messages
        await self._seen_store.async_save(data)

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

    def _fire_message(self, message: Message, change: str) -> None:
        self.hass.bus.async_fire(
            EVENT_NEW_ENTRY,
            {
                "entry_type": "message",
                "change": change,
                "message_type": message.message_type,
                "message_id": message.id,
                "child_ids": list(message.child_ids),
                "child_names": [self.child_name(c) for c in message.child_ids],
                "subject": message.subject,
                "preview": message.preview,
                "author": message.author,
                "from_parent": message.from_parent,
                "has_attachment": message.has_attachment,
                "sent": message.sent.isoformat(),
                "unread": message.unread,
                "url": f"{self.client.base_url}/parent/#/tabs/mailbox",
            },
        )

    def _fire_photos(self, child_id: str, photos: list[Photo]) -> None:
        """One event per batch, so an upload of 20 photos is one notification."""
        # The latest photo is shown to every Home Assistant user by the image entity; older ones only to admins.
        image = er.async_get(self.hass).async_get_entity_id(
            "image", DOMAIN, child_entity_unique_id(self, child_id, LATEST_PHOTO_KEY)
        )
        self.hass.bus.async_fire(
            EVENT_NEW_ENTRY,
            {
                "entry_type": "photos",
                "change": "new",
                "child_id": child_id,
                "child_name": self.child_name(child_id),
                "count": len(photos),
                # The child's latest photo, served by its image entity; the companion app loads it with your login.
                "media_url": f"/api/image_proxy/{image}" if image else None,
                "media_content_id": f"media-source://image/{image}" if image else None,
                # The portal's signed URL; works anywhere but expires after about an hour.
                "image_url": photos[0].medium_url or photos[0].full_url,
                "photos": [
                    {
                        "id": p.id,
                        "date": p.day.isoformat(),
                        "description": p.description,
                        "media_type": p.media_type,
                        # The saved copy, for admins only.
                        "media_url": self.archive.url(p),
                        "full_url": p.full_url,
                        "medium_url": p.medium_url,
                        "thumb_url": p.thumb_url,
                    }
                    for p in photos
                ],
            },
        )


class ScheduleCoordinator(DataUpdateCoordinator[list[Session]]):
    """Reads the children's schedule from the portal's Agenda: the planned sessions, earliest first.

    Apart from the timeline, which changes all day: the schedule is read on its own clock, and when one of the two
    cannot be read, the other's entities stay available.
    """

    config_entry: OuderportaalConfigEntry

    def __init__(self, hass: HomeAssistant, entry: OuderportaalConfigEntry, client: OuderportaalClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} schedule",
            update_interval=SCHEDULE_UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> list[Session]:
        ranges = schedule_ranges(dt_util.now().date(), SCHEDULE_PAST_DAYS, SCHEDULE_FUTURE_DAYS, SCHEDULE_REQUEST_DAYS)
        days: list[dict[str, Any]] = []
        try:
            # One month after the other, like the portal's Agenda. All or nothing, so a month that fails does not
            # look like its days were cancelled, and the months after it are not asked for.
            for start, end in ranges:
                days += await self.client.get_schedule(start, end)
        except OuderportaalAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except OuderportaalError as err:
            raise UpdateFailed(str(err)) from err
        return parse_schedule(days, dt_util.get_default_time_zone())
