"""Each child's day rhythm as a calendar."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import OuderportaalConfigEntry
from .entity import OuderportaalEntity
from .timeline import Moment

# Moments without an end time (a bottle, a diaper) are shown as short blocks.
POINT_DURATION = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OuderportaalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(OuderportaalCalendar(coordinator, child_id, "day_rhythm") for child_id in coordinator.children)


def _to_event(moment: Moment, now: datetime) -> CalendarEvent:
    description = "\n".join(part for part in (moment.description, moment.remarks) if part) or None
    if moment.start is None:
        return CalendarEvent(
            start=moment.day,
            end=moment.day + timedelta(days=1),
            summary=moment.label,
            description=description,
            uid=moment.key,
        )
    if moment.end and moment.end > moment.start:
        end = moment.end
    elif _is_ongoing(moment, now):
        end = max(now, moment.start) + POINT_DURATION
    else:
        end = moment.start + POINT_DURATION
    return CalendarEvent(start=moment.start, end=end, summary=moment.title, description=description, uid=moment.key)


def _is_ongoing(moment: Moment, now: datetime) -> bool:
    if moment.start is None or moment.start > now:
        return False
    if moment.end:
        return now < moment.end
    # A nap logged without an end time today is still going on.
    return moment.category == "sleep" and moment.day == now.date()


class OuderportaalCalendar(OuderportaalEntity, CalendarEntity):
    """Only covers the days on the first timeline page (usually the last day or two)."""

    @property
    def event(self) -> CalendarEvent | None:
        """The moment happening right now, e.g. an ongoing nap."""
        now = dt_util.now()
        for moment in reversed(self.moments):
            if _is_ongoing(moment, now):
                return _to_event(moment, now)
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        now = dt_util.now()
        events = []
        for moment in self.moments:
            event = _to_event(moment, now)
            if event.start_datetime_local < end_date and event.end_datetime_local > start_date:
                events.append(event)
        return events
