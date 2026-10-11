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

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

ATTEND = "attend"
ABSENT = "absent"
TENTATIVE = "tentative"

INTER_CHANGE = "booking.type.inter_change"
CHANGE_GROUP = "booking.type.change_group"

# Sub code -> how the part differs from the usual schedule ('planned').
_REASONS = {
    "extra": "extra day",
    "inter_change": "swap day",
    "change_group": "moved",
}


@dataclass(frozen=True, slots=True)
class Session:
    """A time a child is planned to be at daycare."""

    child_id: str
    start: datetime
    end: datetime
    summary: str
    description: str = ""
    location: str = ""


def parse_schedule(days: list[dict[str, Any]], tz: tzinfo) -> list[Session]:
    """Sessions sorted by start. Absences are left out, as a calendar leaves out cancelled events."""
    sessions: dict[Session, None] = {}
    for day in _dicts(days):
        for planning in _dicts(day.get("children")):
            child_id = (planning.get("child") or {}).get("id")
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
    if start is None or end is None or end <= start or code == ABSENT:
        return None
    group = part.get("childGroup") or {}
    details = [_clean(part.get("extraInfo") or "")]
    if name := _clean(group.get("name") or ""):
        details.append(f"Group: {name}")
    if status := part.get("bookingStatus"):
        # A translation key, e.g. 'booking.status.pending_approval'.
        details.append(f"Status: {status.rsplit('.', 1)[-1].replace('_', ' ').capitalize()}")
    return Session(
        child_id=child_id,
        start=start,
        end=end,
        summary=_summary(part, code, tz),
        description="\n".join(detail for detail in details if detail),
        location=_clean((group.get("location") or {}).get("name") or ""),
    )


def _summary(part: dict[str, Any], code: str, tz: tzinfo) -> str:
    """'Daycare', with what makes the part different in brackets, e.g. 'Daycare (swap day from 7 October)'."""
    notes = []
    if part.get("toConfirmProduct"):
        notes.append("to confirm" if code == TENTATIVE else "confirmed")
    elif code == TENTATIVE:
        notes.append("option")
    booking_type = part.get("bookingType")
    if booking_type == INTER_CHANGE and (swapped := _time(part.get("interchangeFromDate"), tz)):
        notes.append(f"swap day from {swapped.day} {swapped:%B}")
    elif booking_type == CHANGE_GROUP:
        notes.append("moved")
    elif code != TENTATIVE and (reason := _REASONS.get(_code(part.get("plannedAttendanceSubCode")) or "")):
        notes.append(reason)
    return f"Daycare ({', '.join(notes)})" if notes else "Daycare"


def _time(value: Any, tz: tzinfo) -> datetime | None:
    """The Agenda sends times as epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value / 1000, tz)


def _code(value: Any) -> str | None:
    return value.get("code") if isinstance(value, dict) else None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _clean(value: str) -> str:
    return " ".join(value.split())
