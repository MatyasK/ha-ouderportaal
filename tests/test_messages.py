"""The portal's Messages page (conversations, news, newsletters): parsing, notifications, sensors, history import.

The fixtures follow the portal's real answers. News items were empty in the captured traffic, so news.json uses the
fields the portal's own app reads from them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN, EVENT_NEW_ENTRY
from custom_components.ouderportaal.timeline import (
    Message,
    Timeline,
    merge_timelines,
    parse_messages,
    parse_news,
    parse_newsletters,
)

from .conftest import CHILD_ID, load_fixture

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

TZ = ZoneInfo("Europe/Amsterdam")
NOW = "2026-10-07T15:00:00+00:00"
TRIP_TEXT = "Beste ouders,\nMorgen gaan we naar de kinderboerderij. Graag laarsjes mee!\nGroetjes,\nGroep Tommie"


def _news() -> list[dict[str, Any]]:
    return load_fixture("news.json")["payload"]["newsItems"]


def _newsletters() -> list[dict[str, Any]]:
    return load_fixture("newsletters.json")["payload"]["newsletterItems"]


# Parsing


def test_parses_conversations() -> None:
    trip, pacifier = parse_messages(load_fixture("messages.json"), TZ)

    assert trip == Message(
        id="msg-trip",
        subject="Uitje naar de kinderboerderij",
        # Line breaks in the HTML source are not line breaks on screen.
        preview=TRIP_TEXT,
        sent=datetime(2026, 10, 7, 14, 30, tzinfo=TZ),
        unread=True,
        child_ids=(CHILD_ID,),
        message_type="message",
        author="Tommie",
        from_parent=False,
    )
    # A conversation keeps the id of its first message, also after a reply.
    assert pacifier.id == "msg-pacifier"
    assert pacifier.subject == "\U0001f37c Welke speen gebruikt jouw kindje?"
    assert pacifier.preview == "Sam gebruikt een speen van Bibs."
    assert pacifier.author == "Alex Test"
    assert pacifier.from_parent is True
    # The portal sends null for conversations a parent answered last.
    assert pacifier.unread is False


def test_dates_without_a_zone_are_local_time() -> None:
    (message,) = parse_messages([{"logMessageMessageId": "x", "date": "2026-10-07T14:30:00"}], TZ)

    assert message.sent == datetime(2026, 10, 7, 14, 30, tzinfo=TZ)


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
    assert parse_news([{"id": "x", "publishDate": bad_date}], TZ) == []
    assert parse_newsletters([{"generatedHtmlNewsLetterId": "x", "sendDate": bad_date}], TZ) == []


def test_parses_news_items() -> None:
    (news,) = parse_news(_news(), TZ)

    assert news == Message(
        id="news-closed",
        subject="Sluitingsdag 24 oktober",
        preview="Op 24 oktober zijn we gesloten vanwege een studiedag.",
        sent=datetime(2026, 10, 5, tzinfo=TZ),
        unread=True,
        child_ids=(),
        message_type="news",
        has_attachment=True,
    )


@pytest.mark.parametrize(
    ("item", "expected_id"),
    [({"htmlNewsId": "a"}, "a"), ({"htmlNewsItemId": "b"}, "b"), ({"newsItemId": "c"}, "c")],
)
def test_news_id_fallbacks_match_the_portal_app(item: dict[str, Any], expected_id: str) -> None:
    (news,) = parse_news([item | {"publishDate": "2026-10-05"}], TZ)

    assert news.id == expected_id
    assert news.sent == datetime(2026, 10, 5, tzinfo=TZ)


def test_parses_newsletters_and_service_messages() -> None:
    survey, rates = parse_newsletters(_newsletters(), TZ)

    assert survey.id == "nl-survey"
    assert survey.message_type == "newsletter"
    assert survey.subject == "Samen voor de beste kwaliteit - Deel uw ervaring!"
    # A plain text snippet: the '<' is text, not HTML.
    assert survey.preview.startswith("< English version below> Beste ouder(s),")
    assert "geïnformeerd" in survey.preview
    assert survey.sent == datetime(2026, 10, 5, 10, 25, tzinfo=TZ)
    assert survey.unread is True
    assert survey.child_ids == ()
    assert rates.id == "nl-rates"
    assert rates.message_type == "service_message"
    assert rates.unread is False


def test_reading_a_message_does_not_change_its_fingerprint() -> None:
    (unread,) = parse_messages([{"id": "x", "message": "Hoi", "date": "2026-10-07T09:00:00", "unread": True}], TZ)
    (read,) = parse_messages([{"id": "x", "message": "Hoi", "date": "2026-10-07T09:00:00", "unread": False}], TZ)
    (reply,) = parse_messages([{"id": "x", "message": "Dank je", "date": "2026-10-07T10:00:00"}], TZ)

    assert unread.fingerprint == read.fingerprint
    assert reply.fingerprint != read.fingerprint


def test_messages_survive_storage_and_merge() -> None:
    messages = [*parse_messages(load_fixture("messages.json"), TZ), *parse_news(_news(), TZ)]
    # The same id in another kind of item is another item.
    newsletter = parse_newsletters([{"generatedHtmlNewsLetterId": "msg-trip", "sendDate": "2026-10-06T08:00:00"}], TZ)
    stored = Timeline.from_dict(Timeline([], [], messages).to_dict())

    assert stored.messages == messages
    merged = merge_timelines(stored, Timeline([], [], newsletter), since=date(2026, 10, 6))
    assert [m.key for m in merged.messages] == [
        "message-msg-trip",
        "message-msg-pacifier",
        "newsletter-msg-trip",
        "news-news-closed",
    ]


def test_history_from_before_news_and_newsletters_loads() -> None:
    old = {
        "id": "msg-trip",
        "subject": "Uitje",
        "preview": "Laarsjes mee",
        "sent": "2026-10-07T14:30:00+02:00",
        "unread": True,
        "child_ids": [CHILD_ID],
    }

    (message,) = Timeline.from_dict({"messages": [old]}).messages

    assert message.message_type == "message"
    assert message.author == ""
    assert message.from_parent is False


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


def _message_events(events: list[Any]) -> list[dict[str, Any]]:
    return [e.data for e in events if e.data["entry_type"] == "message"]


async def test_mailbox_sensors(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    # The badge on the portal's Messages tab counts conversations, news and newsletters together.
    unread = hass.states.get("sensor.ouderportaal_mailbox_unread_messages")
    assert unread.state == "2"
    assert unread.attributes["messages"] == 1
    assert unread.attributes["news"] == 1
    assert unread.attributes["newsletters"] == 0
    latest = hass.states.get("sensor.ouderportaal_mailbox_latest_message")
    assert latest.state == "Uitje naar de kinderboerderij"
    assert latest.attributes["message_type"] == "message"
    assert latest.attributes["preview"] == TRIP_TEXT
    assert latest.attributes["author"] == "Tommie"
    assert latest.attributes["from_parent"] is False
    assert latest.attributes["unread"] is True
    assert latest.attributes["child_names"] == ["Sam"]


async def test_latest_message_can_be_a_newsletter(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_newsletters.return_value = [_newsletters()[0] | {"sendDate": "2026-10-07T16:00:00"}]

    await _setup(hass, entry)

    latest = hass.states.get("sensor.ouderportaal_mailbox_latest_message")
    assert latest.state == "Samen voor de beste kwaliteit - Deel uw ervaring!"
    assert latest.attributes["message_type"] == "newsletter"
    assert latest.attributes["child_names"] == []


async def test_unread_count_of_null_means_zero(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_notifications.return_value = {
        "nrOfNewMessages": None,
        "nrOfNewNewsItems": None,
        "nrOfNewNewsletters": None,
    }

    await _setup(hass, entry)

    assert hass.states.get("sensor.ouderportaal_mailbox_unread_messages").state == "0"


async def test_new_message_and_reply_are_announced(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)
    assert events == []

    sick = _messages()[0] | {
        "logMessageMessageId": "msg-sick",
        "lastLogMessageMessageId": "msg-sick",
        "subject": "Ziek",
        "message": "<p>Sam heeft verhoging.</p>",
        "date": 1791384300000,  # 16:45
    }
    mock_client.get_messages.return_value = [sick, *_messages()]
    await _refresh(hass, entry)

    (event,) = events
    assert event.data == {
        "entry_type": "message",
        "change": "new",
        "message_type": "message",
        "message_id": "msg-sick",
        "child_ids": [CHILD_ID],
        "child_names": ["Sam"],
        "subject": "Ziek",
        "preview": "Sam heeft verhoging.",
        "author": "Tommie",
        "from_parent": False,
        "has_attachment": False,
        "sent": "2026-10-07T16:45:00+02:00",
        "unread": True,
        "url": "https://mijnopvang.ouderportaal.nl/parent/#/tabs/mailbox",
    }

    # A reply in an existing conversation: same id, newer date and text.
    mock_client.get_messages.return_value = _messages(
        {"lastLogMessageMessageId": "msg-trip-reply", "message": "<p>Fijn, tot morgen!</p>", "date": 1791385200000}
    )
    await _refresh(hass, entry)

    assert events[-1].data["change"] == "updated"
    assert events[-1].data["message_id"] == "msg-trip"
    assert events[-1].data["preview"] == "Fijn, tot morgen!"


async def test_reply_by_a_parent_is_marked_as_such(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.get_messages.return_value = _messages(
        {
            "lastLogMessageMessageId": "msg-trip-reply",
            "message": "<p>Laarsjes zitten in de tas.</p>",
            "lastWritten": "Alex Test",
            "type": "from_parent_message",
            "lastFromParent": True,
            "unread": None,
            "date": 1791385200000,
        }
    )
    await _refresh(hass, entry)

    (event,) = _message_events(events)
    assert event["change"] == "updated"
    assert event["author"] == "Alex Test"
    assert event["from_parent"] is True
    assert event["unread"] is False


async def test_new_news_and_newsletters_are_announced(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.get_news.return_value = [
        _news()[0] | {"id": "news-party", "title": "Herfstfeest", "publishDate": "2026-10-07"},
        *_news(),
    ]
    closing = {"generatedHtmlNewsLetterId": "nl-closing", "mailSubject": "Sluiting", "sendDate": 1791385200000}
    mock_client.get_newsletters.return_value = [_newsletters()[1] | closing, *_newsletters()]
    await _refresh(hass, entry)

    news, service = _message_events(events)
    assert news["message_type"] == "news"
    assert news["message_id"] == "news-party"
    assert news["subject"] == "Herfstfeest"
    assert news["has_attachment"] is True
    assert news["child_ids"] == []
    assert news["sent"] == "2026-10-07T00:00:00+02:00"
    assert service["message_type"] == "service_message"
    assert service["message_id"] == "nl-closing"
    assert service["change"] == "new"


async def test_reading_messages_is_not_announced(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.get_messages.return_value = _messages({"unread": False})
    mock_client.get_news.return_value = [_news()[0] | {"isNew": False}]
    mock_client.get_newsletters.return_value = [item | {"unread": False} for item in _newsletters()]
    await _refresh(hass, entry)

    assert events == []


@pytest.mark.parametrize(
    "seen",
    [
        # Before messages.
        {"moments": {}, "photos": []},
        # Before news and newsletters: conversations only, by their bare id.
        {"moments": {}, "photos": [], "messages": {"msg-trip": "0123456789ab"}},
    ],
)
async def test_upgrade_is_silent(hass: HomeAssistant, entry, mock_client, hass_storage, seen) -> None:
    hass_storage[f"ouderportaal.{entry.entry_id}.seen"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"ouderportaal.{entry.entry_id}.seen",
        "data": seen,
    }
    events = async_capture_events(hass, EVENT_NEW_ENTRY)

    await _setup(hass, entry)
    await _refresh(hass, entry)

    assert _message_events(events) == []


async def test_only_what_the_account_may_see_is_read(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_authorizations.return_value = {"WEB_PARENT_LOGBOOK", "WEB_JOURNAL_PARENT_VIEW"}

    await _setup(hass, entry)

    mock_client.get_news.assert_not_awaited()
    mock_client.get_newsletters.assert_not_awaited()
    assert {m.message_type for m in entry.runtime_data.history.messages} == {"message"}


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


async def test_one_failing_part_does_not_announce_the_rest_again(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.get_newsletters.side_effect = OuderportaalConnectionError("newsletters down")
    await _refresh(hass, entry)
    mock_client.get_newsletters.side_effect = None
    await _refresh(hass, entry)

    assert _message_events(events) == []


async def test_rejected_password_on_messages_starts_reauth(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)
    mock_client.get_news.side_effect = OuderportaalAuthError("password changed")

    await _refresh(hass, entry)

    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]


async def test_unreadable_authorizations_retry_setup(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_authorizations.side_effect = OuderportaalConnectionError("down")

    await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_history_import_includes_30_days_of_messages(hass: HomeAssistant, entry, mock_client) -> None:
    older = _messages()[0] | {"logMessageMessageId": "msg-old", "subject": "Ouderavond", "date": "2026-09-10T12:00:00"}
    too_old = older | {"logMessageMessageId": "msg-too-old", "date": "2026-08-01T12:00:00"}
    older_newsletter = _newsletters()[0] | {"generatedHtmlNewsLetterId": "nl-old", "sendDate": "2026-09-12T09:00:00"}

    def window(recent: list[dict[str, Any]], *old: dict[str, Any]):
        return lambda start, end: [*recent, *old] if start < date(2026, 9, 30) else recent

    mock_client.get_messages.side_effect = window(_messages(), older, too_old)
    mock_client.get_newsletters.side_effect = window(_newsletters(), older_newsletter)

    await _setup(hass, entry)

    assert [m.key for m in entry.runtime_data.history.messages] == [
        "message-msg-trip",
        "message-msg-pacifier",
        "newsletter-nl-survey",
        "news-news-closed",
        "service_message-nl-rates",
        "newsletter-nl-old",
        "message-msg-old",
    ]
    for fetch in (mock_client.get_messages, mock_client.get_news, mock_client.get_newsletters):
        windows = [(c.args[-2], c.args[-1]) for c in fetch.await_args_list]
        assert (date(2026, 9, 30), date(2026, 10, 8)) in windows  # the poll: last 7 days
        assert (date(2026, 9, 7), date(2026, 10, 8)) in windows  # the import: last 30 days
