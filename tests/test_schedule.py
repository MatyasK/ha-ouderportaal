"""The schedule from the portal's Agenda: parsing, the calendar, and how often it is read.

schedule.json is the portal's real answer for four weeks of a child going on Wednesdays and Thursdays. Absences, extra
days, swap days and options were not in the captured traffic, so those parts use the fields the portal's own app reads.
"""

from __future__ import annotations

import copy
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN
from custom_components.ouderportaal.schedule import INTER_CHANGE, Session, parse_schedule

from .conftest import CHILD_ID, TZ, load_fixture

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# Wednesday 13:00 in Amsterdam, after that morning's session.
NOW = "2026-10-07T11:00:00+00:00"


def _days() -> list[dict[str, Any]]:
    return load_fixture("schedule.json")["payload"]["days"]


def _at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def _millis(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def _code(code: str) -> dict[str, str]:
    return {"code": code, "label": f"planned.attendance.code.{code}"}


def _sub_code(code: str) -> dict[str, str]:
    return {"code": code, "label": f"planned.attendance.sub.code.{code}"}


def _part(start: datetime | None = None, end: datetime | None = None, **changes: Any) -> dict[str, Any]:
    """A planning part like the fixture's (Thursday 8 October, 07:00-18:00 as planned), with some fields changed."""
    part = copy.deepcopy(_days()[1]["children"][0]["combinedPlanningParts"][0]["planningParts"][0])
    if start:
        part["startTime"] = _millis(start)
    if end:
        part["endTime"] = _millis(end)
    return part | changes


def _day(*parts: dict[str, Any]) -> dict[str, Any]:
    """An Agenda day for Sam holding these parts, dated like the portal does: the day, at the time of the request."""
    day = copy.deepcopy(_days()[1])
    start = datetime.fromtimestamp(parts[0]["startTime"] / 1000, TZ)
    day["date"] = _millis(datetime.combine(start.date(), time(16, 33), TZ))
    day["dayOfMonth"] = start.day
    day["children"][0]["combinedPlanningParts"] = [{"planningParts": list(parts), "subscriptions": []}]
    return day


def _absent(**changes: Any) -> dict[str, Any]:
    return {"attend": False, "plannedAttendanceCode": _code("absent")} | changes


# Parsing


def test_parses_planned_days() -> None:
    sessions = parse_schedule(_days(), TZ)

    assert sessions[0] == Session(
        child_id=CHILD_ID,
        start=_at(7, 7),
        end=_at(7, 12, 30),
        summary="Daycare",
    )
    # The days are dated at the time of the request, not at midnight; the times are what counts.
    assert [s.start.isoformat() for s in sessions] == [
        "2026-10-07T07:00:00+02:00",
        "2026-10-08T07:00:00+02:00",
        "2026-10-14T07:00:00+02:00",
        "2026-10-15T07:00:00+02:00",
        "2026-10-21T07:00:00+02:00",
        "2026-10-22T07:00:00+02:00",
        "2026-10-28T07:00:00+01:00",
        "2026-10-29T07:00:00+01:00",
    ]
    assert {s.end.time() for s in sessions[1:]} == {time(18)}


@pytest.mark.parametrize(
    ("changes", "summary"),
    [
        ({}, "Daycare"),
        ({"plannedAttendanceSubCode": _sub_code("extra"), "bookingType": "booking.type.extra"}, "Daycare (extra day)"),
        (
            {
                "plannedAttendanceSubCode": _sub_code("inter_change"),
                "bookingType": INTER_CHANGE,
                "interchangeFromDate": _millis(_at(5)),
            },
            "Daycare (swap day from 5 October)",
        ),
        (
            {"plannedAttendanceSubCode": _sub_code("change_group"), "bookingType": "booking.type.change_group"},
            "Daycare (moved)",
        ),
        ({"plannedAttendanceCode": _code("tentative"), "option": True}, "Daycare (option)"),
        (
            {
                "plannedAttendanceCode": _code("tentative"),
                "bookingType": INTER_CHANGE,
                "interchangeFromDate": _millis(_at(5)),
            },
            "Daycare (option, swap day from 5 October)",
        ),
        (
            {"plannedAttendanceCode": _code("tentative"), "toConfirmProduct": {"canChangeStatus": True}},
            "Daycare (to confirm)",
        ),
        ({"toConfirmProduct": {"canChangeStatus": True}}, "Daycare (confirmed)"),
        # The portal has no text for some sub codes, such as 'manual'.
        ({"plannedAttendanceSubCode": _sub_code("manual")}, "Daycare"),
    ],
)
def test_summary_says_what_is_different(changes: dict[str, Any], summary: str) -> None:
    (session,) = parse_schedule([_day(_part(**changes))], TZ)

    assert session.summary == summary


@pytest.mark.parametrize(
    "changes",
    [
        _absent(plannedAttendanceSubCode=_sub_code("illness"), absentType="illness", bookingType="booking.type.absent"),
        _absent(plannedAttendanceSubCode=_sub_code("vacation"), absentType="vacation"),
        # Swapped to another day.
        _absent(bookingType=INTER_CHANGE, interchangeToDate=_millis(_at(9))),
        # A day to confirm, withdrawn.
        _absent(toConfirmProduct={"canChangeStatus": True}),
        # Without a code, the portal's 'attend' flag.
        {"plannedAttendanceCode": None, "attend": False},
    ],
)
def test_absences_are_left_out(changes: dict[str, Any]) -> None:
    assert parse_schedule([_day(_part(**changes))], TZ) == []


def test_group_location_and_status_are_details() -> None:
    (session,) = parse_schedule(
        [
            _day(
                _part(
                    plannedAttendanceSubCode=_sub_code("extra"),
                    bookingType="booking.type.extra",
                    bookingStatus="booking.status.pending_approval",
                    bookingId="booking-1",
                    childGroup={"id": "group-1", "name": "Tommie", "location": {"id": "loc-1", "name": "De  Test KDV"}},
                )
            )
        ],
        TZ,
    )

    assert session.summary == "Daycare (extra day)"
    assert session.description == "Group: Tommie\nStatus: Pending approval"
    assert session.location == "De Test KDV"


def test_option_text_is_a_detail() -> None:
    (session,) = parse_schedule(
        [_day(_part(plannedAttendanceCode=_code("tentative"), option=True, extraInfo="Flexibele  opvang"))], TZ
    )

    assert session.summary == "Daycare (option)"
    assert session.description == "Flexibele opvang"


def test_each_part_of_a_day_is_a_session() -> None:
    # Before and after school, which the portal shows as '07:30-08:30 & 14:00-18:00'.
    day = _day(_part(_at(8, 7, 30), _at(8, 8, 30)), _part(_at(8, 14), _at(8, 18)))

    sessions = parse_schedule([day], TZ)

    assert [(s.start, s.end) for s in sessions] == [(_at(8, 7, 30), _at(8, 8, 30)), (_at(8, 14), _at(8, 18))]


def test_unusable_parts_are_skipped() -> None:
    day = _day(_part(), _part(startTime=None), _part(endTime="18:00"), _part(_at(8, 18), _at(8, 7)))
    day["children"][0]["combinedPlanningParts"][0]["planningParts"].append("junk")
    no_child = copy.deepcopy(day) | {"children": [{"child": None, "combinedPlanningParts": []}]}

    sessions = parse_schedule([day, no_child, "junk", {"date": 1791383603139}], TZ)

    assert [(s.start, s.end) for s in sessions] == [(_at(8, 7), _at(8, 18))]


def test_a_day_in_two_answers_is_one_session() -> None:
    assert len(parse_schedule(_days()[:1] * 2, TZ)) == 1


# In Home Assistant


@pytest.fixture
async def entry(hass: HomeAssistant, freezer, media_dir, photo_cdn) -> MockConfigEntry:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mijnopvang:parent@example.com",
        data={CONF_PORTAL: "mijnopvang", CONF_USERNAME: "parent@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _events(hass: HomeAssistant, start: str, days: int) -> list[dict[str, Any]]:
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.sam_schedule", "start_date_time": start, "duration": {"days": days}},
        blocking=True,
        return_response=True,
    )
    return result["calendar.sam_schedule"]["events"]


def _times(events: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(e["summary"], e["start"], e["end"]) for e in events]


async def test_calendar_shows_the_planned_days(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    assert _times(await _events(hass, "2026-10-05 00:00", 14)) == [
        ("Daycare", "2026-10-07T07:00:00+02:00", "2026-10-07T12:30:00+02:00"),
        ("Daycare", "2026-10-08T07:00:00+02:00", "2026-10-08T18:00:00+02:00"),
        ("Daycare", "2026-10-14T07:00:00+02:00", "2026-10-14T18:00:00+02:00"),
        ("Daycare", "2026-10-15T07:00:00+02:00", "2026-10-15T18:00:00+02:00"),
    ]


async def test_swap_day_moves_the_session(hass: HomeAssistant, entry, mock_client) -> None:
    days = _days()
    # Wednesday 14 October swapped to Friday 16 October.
    days[2] = _day(
        _part(_at(14, 7), _at(14, 18), **_absent(bookingType=INTER_CHANGE, interchangeToDate=_millis(_at(16))))
    )
    swap = _part(
        _at(16, 7),
        _at(16, 18),
        plannedAttendanceSubCode=_sub_code("inter_change"),
        bookingType=INTER_CHANGE,
        interchangeFromDate=_millis(_at(14)),
        bookingStatus="booking.status.approved",
        childGroup={"id": "group-1", "name": "Tommie", "location": {"id": "loc-1", "name": "De Test KDV"}},
    )
    mock_client.schedule = [*days[:4], _day(swap), *days[4:]]

    await _setup(hass, entry)

    events = await _events(hass, "2026-10-12 00:00", 7)
    assert _times(events) == [
        ("Daycare", "2026-10-15T07:00:00+02:00", "2026-10-15T18:00:00+02:00"),
        ("Daycare (swap day from 14 October)", "2026-10-16T07:00:00+02:00", "2026-10-16T18:00:00+02:00"),
    ]
    assert events[1]["description"] == "Group: Tommie\nStatus: Approved"
    assert events[1]["location"] == "De Test KDV"


async def test_calendar_is_on_while_at_daycare(hass: HomeAssistant, entry, mock_client, freezer) -> None:
    await _setup(hass, entry)

    # Wednesday's session ended at 12:30, so the calendar points at Thursday's.
    state = hass.states.get("calendar.sam_schedule")
    assert state.state == STATE_OFF
    assert state.attributes["message"] == "Daycare"
    assert state.attributes["start_time"] == "2026-10-08 07:00:00"
    assert state.attributes["end_time"] == "2026-10-08 18:00:00"

    freezer.move_to("2026-10-08T05:00:00+00:00")  # 07:00 in Amsterdam
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert hass.states.get("calendar.sam_schedule").state == STATE_ON


async def test_reads_a_month_back_to_three_months_ahead(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    # A month per request, like the portal's Agenda.
    assert [tuple(c.args[-2:]) for c in mock_client.get_schedule.await_args_list] == [
        (date(2026, 9, 6), date(2026, 10, 6)),
        (date(2026, 10, 7), date(2026, 11, 6)),
        (date(2026, 11, 7), date(2026, 12, 7)),
        (date(2026, 12, 8), date(2027, 1, 7)),
    ]


async def test_schedule_is_read_every_half_hour(hass: HomeAssistant, entry, mock_client, freezer) -> None:
    await _setup(hass, entry)
    mock_client.get_schedule.reset_mock()
    # Thursday is reported ill on the portal.
    days = _days()
    days[1] = _day(_part(**_absent(plannedAttendanceSubCode=_sub_code("illness"), absentType="illness")))
    mock_client.schedule = days

    freezer.tick(timedelta(minutes=29))
    await _refresh(hass, entry)
    mock_client.get_schedule.assert_not_awaited()
    assert hass.states.get("calendar.sam_schedule").attributes["start_time"] == "2026-10-08 07:00:00"

    freezer.tick(timedelta(minutes=1))
    await _refresh(hass, entry)
    assert mock_client.get_schedule.await_count == 4
    assert hass.states.get("calendar.sam_schedule").attributes["start_time"] == "2026-10-14 07:00:00"


async def test_failing_schedule_keeps_the_last_one(hass: HomeAssistant, entry, mock_client, freezer, caplog) -> None:
    await _setup(hass, entry)
    serve_schedule = mock_client.get_schedule.side_effect
    mock_client.get_schedule.side_effect = OuderportaalConnectionError("agenda down")
    caplog.set_level(logging.INFO)

    freezer.tick(timedelta(minutes=30))
    await _refresh(hass, entry)
    freezer.tick(timedelta(minutes=5))
    await _refresh(hass, entry)

    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.sam_last_activity").state == "12:00 (Fles)voeding"
    assert len(await _events(hass, "2026-10-05 00:00", 14)) == 4
    # Tried on every poll, warned once.
    assert mock_client.get_schedule.await_count == 4 + 2
    assert caplog.text.count("Could not read the schedule") == 1

    mock_client.get_schedule.side_effect = serve_schedule
    freezer.tick(timedelta(minutes=5))
    await _refresh(hass, entry)
    assert "Reading the schedule works again" in caplog.text


async def test_unreadable_schedule_does_not_stop_setup(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_schedule.side_effect = OuderportaalConnectionError("agenda down")

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("calendar.sam_schedule").state == STATE_OFF
    assert await _events(hass, "2026-10-05 00:00", 14) == []


async def test_rejected_password_on_schedule_starts_reauth(hass: HomeAssistant, entry, mock_client, freezer) -> None:
    await _setup(hass, entry)
    mock_client.get_schedule.side_effect = OuderportaalAuthError("password changed")

    freezer.tick(timedelta(minutes=30))
    await _refresh(hass, entry)

    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]


@pytest.mark.parametrize(
    ("authorizations", "has_schedule"),
    [
        ({"WEB_PHOTOS_RECENT", "WEB_JOURNAL_PARENT_VIEW", "WEB_PARENT_LOGBOOK"}, False),
        # The portal's Agenda tab also shows for parent meetings only.
        ({"WEB_JOURNAL_PARENT_VIEW", "WEB_PARENT_MEETINGS_VIEW"}, True),
    ],
)
async def test_schedule_only_with_the_agenda(
    hass: HomeAssistant, entry, mock_client, authorizations: set[str], has_schedule: bool
) -> None:
    mock_client.get_authorizations.return_value = authorizations

    await _setup(hass, entry)

    assert (hass.states.get("calendar.sam_schedule") is not None) is has_schedule
    assert mock_client.get_schedule.await_count == (4 if has_schedule else 0)
    assert hass.states.get("calendar.sam_day_rhythm") is not None
