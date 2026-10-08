"""Mailbox messages: parsing, notifications, sensors and the history import."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN, EVENT_NEW_ENTRY
from custom_components.ouderportaal.timeline import Timeline, merge_timelines, parse_messages

from .conftest import CHILD_ID, load_fixture

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

TZ = ZoneInfo("Europe/Amsterdam")
NOW = "2026-10-07T15:00:00+00:00"


# Parsing


def test_parses_overview_items_newest_first() -> None:
    trip, closed = parse_messages(load_fixture("messages.json"), TZ)

    assert trip.id == "msg-trip"
    assert trip.subject == "Uitje naar de kinderboerderij"
    assert trip.preview == "Morgen gaan we naar de kinderboerderij.\nGraag laarsjes mee!"
    # A date without a zone is local time.
    assert trip.sent == datetime(2026, 10, 7, 14, 30, tzinfo=TZ)
    assert trip.unread is True
    assert trip.child_ids == (CHILD_ID,)
    # Epoch milliseconds work too.
    assert closed.sent == datetime(2026, 10, 5, 12, 30, tzinfo=TZ)
    assert closed.child_ids == ()


@pytest.mark.parametrize(
    ("item", "expected_id"),
    [
        ({"logbookMessageId": "a"}, "a"),
        ({"logMessageId": "b"}, "b"),
        ({"id": 42}, "42"),
        ({"subject": "Hoi"}, "2026-10-07T09:00:00+02:00-Hoi"),
    ],
)
def test_message_id_fallbacks_match_the_portal_app(item: dict[str, Any], expected_id: str) -> None:
    (message,) = parse_messages([item | {"date": "2026-10-07T09:00:00"}], TZ)

    assert message.id == expected_id


@pytest.mark.parametrize("bad_date", [None, "", "not a date", True])
def test_items_without_usable_date_are_skipped(bad_date: Any) -> None:
    assert parse_messages([{"logMessageMessageId": "x", "date": bad_date}, "junk"], TZ) == []


def test_reading_a_message_does_not_change_its_fingerprint() -> None:
    (unread,) = parse_messages([{"id": "x", "message": "Hoi", "date": "2026-10-07T09:00:00", "unread": True}], TZ)
    (read,) = parse_messages([{"id": "x", "message": "Hoi", "date": "2026-10-07T09:00:00", "unread": False}], TZ)
    (reply,) = parse_messages([{"id": "x", "message": "Dank je", "date": "2026-10-07T10:00:00"}], TZ)

    assert unread.fingerprint == read.fingerprint
    assert reply.fingerprint != read.fingerprint


def test_messages_survive_storage_and_merge() -> None:
    messages = parse_messages(load_fixture("messages.json"), TZ)
    stored = Timeline.from_dict(Timeline([], [], messages).to_dict())

    assert stored.messages == messages
    merged = merge_timelines(stored, Timeline([], [], messages[:1]), since=date(2026, 10, 6))
    assert [m.id for m in merged.messages] == ["msg-trip", "msg-closed"]


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


def _messages(*changes: dict[str, Any]) -> list[dict[str, Any]]:
    items = load_fixture("messages.json")
    for change in changes:
        items[0] = items[0] | change
    return items


async def test_mailbox_sensors(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    assert hass.states.get("sensor.ouderportaal_mailbox_unread_messages").state == "1"
    latest = hass.states.get("sensor.ouderportaal_mailbox_latest_message")
    assert latest.state == "Uitje naar de kinderboerderij"
    assert latest.attributes["preview"] == "Morgen gaan we naar de kinderboerderij.\nGraag laarsjes mee!"
    assert latest.attributes["unread"] is True
    assert latest.attributes["child_names"] == ["Sam"]


async def test_unread_count_of_null_means_zero(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_notifications.return_value = {"nrOfNewMessages": None}

    await _setup(hass, entry)

    assert hass.states.get("sensor.ouderportaal_mailbox_unread_messages").state == "0"


async def test_new_message_and_reply_are_announced(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)
    assert events == []

    mock_client.get_messages.return_value = [
        {
            "logMessageMessageId": "msg-sick",
            "subject": "Ziek",
            "message": "Sam heeft verhoging.",
            "date": "2026-10-07T16:45:00",
            "unread": True,
            "children": [{"id": CHILD_ID}],
        },
        *_messages(),
    ]
    await _refresh(hass, entry)

    (event,) = events
    assert event.data == {
        "entry_type": "message",
        "change": "new",
        "message_id": "msg-sick",
        "child_ids": [CHILD_ID],
        "child_names": ["Sam"],
        "subject": "Ziek",
        "preview": "Sam heeft verhoging.",
        "sent": "2026-10-07T16:45:00+02:00",
        "unread": True,
        "url": "https://mijnopvang.ouderportaal.nl/parent/#/tabs/mailbox",
    }

    # A reply in an existing conversation: same id, newer date and preview.
    mock_client.get_messages.return_value = _messages({"message": "Fijn, tot morgen!", "date": "2026-10-07T17:00:00"})
    await _refresh(hass, entry)

    assert events[-1].data["change"] == "updated"
    assert events[-1].data["message_id"] == "msg-trip"
    assert events[-1].data["preview"] == "Fijn, tot morgen!"


async def test_reading_a_message_is_not_announced(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.get_messages.return_value = _messages({"unread": False})
    await _refresh(hass, entry)

    assert events == []


async def test_upgrade_from_version_without_messages_is_silent(
    hass: HomeAssistant, entry, mock_client, hass_storage
) -> None:
    hass_storage[f"ouderportaal.{entry.entry_id}.seen"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"ouderportaal.{entry.entry_id}.seen",
        "data": {"moments": {}, "photos": []},
    }
    events = async_capture_events(hass, EVENT_NEW_ENTRY)

    await _setup(hass, entry)

    assert [e.data["entry_type"] for e in events if e.data["entry_type"] == "message"] == []


async def test_failing_messages_do_not_break_the_timeline(hass: HomeAssistant, entry, mock_client, caplog) -> None:
    await _setup(hass, entry)
    mock_client.get_messages.side_effect = OuderportaalConnectionError("mailbox down")
    caplog.set_level(logging.WARNING)

    await _refresh(hass, entry)
    await _refresh(hass, entry)

    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.sam_last_activity").state == "12:00 (Fles)voeding"
    # Still showing what it knew, and warned only once.
    assert hass.states.get("sensor.ouderportaal_mailbox_latest_message").state == "Uitje naar de kinderboerderij"
    assert caplog.text.count("Could not read messages") == 1


async def test_rejected_password_on_messages_starts_reauth(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)
    mock_client.get_messages.side_effect = OuderportaalAuthError("password changed")

    await _refresh(hass, entry)

    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]


async def test_history_import_includes_30_days_of_messages(hass: HomeAssistant, entry, mock_client) -> None:
    older = {
        "logMessageMessageId": "msg-old",
        "subject": "Ouderavond",
        "message": "Uitnodiging",
        "date": "2026-09-10T12:00:00",
    }
    too_old = older | {"logMessageMessageId": "msg-too-old", "date": "2026-08-01T12:00:00"}
    mock_client.get_messages.side_effect = lambda start, end: (
        [*_messages(), older, too_old] if start < date(2026, 9, 30) else _messages()
    )

    await _setup(hass, entry)

    assert [m.id for m in entry.runtime_data.history.messages] == ["msg-trip", "msg-closed", "msg-old"]
    windows = [(c.args[-2], c.args[-1]) for c in mock_client.get_messages.await_args_list]
    assert (date(2026, 9, 30), date(2026, 10, 8)) in windows  # the poll: last 7 days
    assert (date(2026, 9, 7), date(2026, 10, 8)) in windows  # the import: last 30 days
