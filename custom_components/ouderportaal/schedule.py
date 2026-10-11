"""Parse the portal's Agenda into the times each child is planned to be at daycare.

Kept free of Home Assistant imports so it can be tested and used standalone.

GET /calendar/<from>/until/<to> lists the days that have something planned. Per child, a day holds planning parts:

    {"startTime": 1791349200000, "endTime": 1791369000000,
     "plannedAttendanceCode": {"code": "attend"}, "plannedAttendanceSubCode": {"code": "planned"},
     "bookingType": null, "bookingStatus": null, "absentType": null, "childGroup": null, ...}

The code is 'attend', 'absent' (illness, vacation, or a day swapped away) or 'tentative' (an option, or a day still to
confirm). The summaries follow what the portal's Agenda shows for a part.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

# Every ouderportaal is Dutch; its /parent answer says 'Europe/Amsterdam'. A date without a time is midnight there.
PORTAL_TZ = ZoneInfo("Europe/Amsterdam")

ATTEND = "attend"
ABSENT = "absent"
TENTATIVE = "tentative"

EXTRA = "booking.type.extra"
INTER_CHANGE = "booking.type.inter_change"
CHANGE_GROUP = "booking.type.change_group"

# A request in one of these states will not happen, so it is left out like an absence.
_UNDONE = frozenset({"rejected", "expired", "approval_error", "deleted", "cancelled"})
# A request in one of these states is not certain yet, which the summary says.
_PENDING = frozenset({"pending_approval", "on_hold", "pending_parent_approval"})

# Sub code -> how the part differs from the usual schedule ('planned').
_REASONS = {
    "extra": "extra day",
    "inter_change": "swap day",
    "change_group": "moved",
}

# Not strftime's %B, which follows the system's language.
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class Session:
    """A time a child is planned to be at daycare."""

    child_id: str
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    location: str = ""


def schedule_ranges(today: date, past_days: int, future_days: int, request_days: int) -> list[tuple[date, date]]:
    """The date ranges to ask for, from past_days back to future_days ahead, each at most request_days long."""
    ranges = []
    start, end = today - timedelta(days=past_days), today + timedelta(days=future_days)
    while start <= end:
        until = min(start + timedelta(days=request_days - 1), end)
        ranges.append((start, until))
        start = until + timedelta(days=1)
    return ranges


def parse_schedule(days: list[dict[str, Any]], tz: tzinfo) -> list[Session]:
    """Sessions sorted by start.

    Absences and requests that will not happen are left out, as a calendar leaves out cancelled events. A field of an
    unexpected type counts as missing, so a surprise from the portal costs at most that one part.
    """
    sessions: dict[Session, None] = {}
    for day in _dicts(days):
        for planning in _dicts(day.get("children")):
            child_id = _str(_dict(planning.get("child")).get("id"))
            if not child_id:
                continue
            for combined in _dicts(planning.get("combinedPlanningParts")):
                for part in _dicts(combined.get("planningParts")):
                    if session := _session(child_id, part, tz):
                        sessions[session] = None
    return sorted(sessions, key=lambda s: (s.start, s.child_id))


def _session(child_id: str, part: dict[str, Any], tz: tzinfo) -> Session | None:
    start, end = _time(part.get("startTime"), tz), _time(part.get("endTime"), tz)
    code = _code(part.get("plannedAttendanceCode")) or (ATTEND if part.get("attend") else ABSENT)
    # A translation key, e.g. 'booking.status.pending_approval'.
    status = _str(part.get("bookingStatus")).rsplit(".", 1)[-1]
    if start is None or end is None or end <= start or code == ABSENT or status in _UNDONE:
        return None
    group = _dict(part.get("childGroup"))
    details = [_str(part.get("extraInfo"))]
    if name := _str(group.get("name")):
        details.append(f"Group: {name}")
    if status:
        details.append(f"Status: {_label(status)}")
    return Session(
        child_id=child_id,
        start=start,
        end=end,
        summary=_summary(part, code, status),
        description="\n".join(detail for detail in details if detail),
        location=_str(_dict(group.get("location")).get("name")),
    )


def _summary(part: dict[str, Any], code: str, status: str) -> str:
    """'Daycare', with what makes the part different in brackets, e.g. 'Daycare (swap day from 7 October)'."""
    notes = []
    if part.get("toConfirmProduct"):
        notes.append("to confirm" if code == TENTATIVE else "confirmed")
    elif code == TENTATIVE:
        notes.append("option")
    booking_type = _str(part.get("bookingType"))
    if booking_type == INTER_CHANGE and (swapped := _time(part.get("interchangeFromDate"), PORTAL_TZ)):
        notes.append(f"swap day from {swapped.day} {_MONTHS[swapped.month - 1]}")
    elif booking_type == CHANGE_GROUP:
        notes.append("moved")
    elif booking_type == EXTRA:
        notes.append("extra day")
    elif code != TENTATIVE and (reason := _REASONS.get(_code(part.get("plannedAttendanceSubCode")))):
        notes.append(reason)
    if status in _PENDING:
        notes.append(_label(status).lower())
    return f"Daycare ({', '.join(notes)})" if notes else "Daycare"


def _label(status: str) -> str:
    """'pending_approval' -> 'Pending approval', as the portal's English texts have it."""
    return status.replace("_", " ").capitalize()


def _time(value: Any, tz: tzinfo) -> datetime | None:
    """The Agenda sends times as epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz)
    except (OverflowError, OSError, ValueError):
        return None


def _code(value: Any) -> str:
    return _str(_dict(value).get("code"))


def _str(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
