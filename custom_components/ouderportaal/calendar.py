"""Each child's day rhythm and schedule as calendars."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import OuderportaalConfigEntry
from .entity import OuderportaalEntity
from .schedule import Session
from .timeline import Moment

# Moments without an end time (a bottle, a diaper) are shown as short blocks.
POINT_DURATION = timedelta(minutes=15)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OuderportaalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[CalendarEntity] = [
        OuderportaalCalendar(coordinator, child_id, "day_rhythm") for child_id in coordinator.children
    ]
    if coordinator.has_schedule:
        entities += [ScheduleCalendar(coordinator, child_id, "schedule") for child_id in coordinator.children]
    async_add_entities(entities)


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
    """The day rhythm history: everything logged since the integration was set up, and the 30 days before."""

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


def _session_event(session: Session) -> CalendarEvent:
    return CalendarEvent(
        start=session.start,
        end=session.end,
        summary=session.summary,
        description=session.description or None,
        location=session.location or None,
        uid=f"{session.child_id}:{session.start.isoformat()}",
    )


class ScheduleCalendar(OuderportaalEntity, CalendarEntity):
    """When the child is planned at daycare, from a month back to three months ahead; "on" while they are there."""

    @property
    def event(self) -> CalendarEvent | None:
        """The session going on now, or else the next one."""
        now = dt_util.now()
        return next((_session_event(s) for s in self.schedule if s.end > now), None)

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [_session_event(s) for s in self.schedule if s.start < end_date and s.end > start_date]
