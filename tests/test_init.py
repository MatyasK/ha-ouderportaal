"""Setting up the integration: entities, events, persistence and failure handling."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN, EVENT_NEW_ENTRY

from .conftest import CHILD_ID

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

# 13:00 in Amsterdam on the fixture's day.
NOW = "2026-10-07T11:00:00+00:00"


def _moment(icon: str, title: str, description: str = "") -> str:
    return (
        f"<table class='day-rythm-moment'><tr><td><div class='icon dagritme-{icon} day-rythm-journal-icon'></div></td>"
        f"<td><div class='moment-title'><span class='moment-title-span'>{title}</span></div>"
        f"<div class='moment-entry-description'>{description}</div><div class='moment-remarks'></div></td></tr></table>"
    )


def _with(cards: list[dict[str, Any]], *moments: str, photo_ids: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    cards = copy.deepcopy(cards)
    cards[0]["journal"]["dayRythmContent"] += "".join(moments)
    template = cards[1]["photos"][0]
    cards[1]["photos"] = [template | {"id": pid} for pid in photo_ids] + cards[1]["photos"]
    return cards


@pytest.fixture
async def entry(hass: HomeAssistant, freezer, media_dir, photo_cdn) -> MockConfigEntry:
    freezer.move_to(NOW)
    await hass.config.async_set_time_zone("Europe/Amsterdam")
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mijnopvang:parent@example.com",
        title="mijnopvang.ouderportaal.nl",
        data={CONF_PORTAL: "mijnopvang", CONF_USERNAME: "parent@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    # Includes the background history import.
    await hass.async_block_till_done(wait_background_tasks=True)


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_entities_reflect_the_day(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    last = hass.states.get("sensor.sam_last_activity")
    assert last.state == "12:00 (Fles)voeding"
    assert last.attributes["amount_ml"] == 140
    assert hass.states.get("sensor.sam_last_bottle").state == "2026-10-07T10:00:00+00:00"
    sleep = hass.states.get("sensor.sam_last_sleep")
    assert sleep.state == "2026-10-07T08:48:00+00:00"
    assert sleep.attributes["sleeping"] is False
    assert hass.states.get("sensor.sam_last_diaper").state == "unknown"
    assert hass.states.get("sensor.sam_milk_today").state == "270.0"
    assert hass.states.get("sensor.sam_sleep_today").state == "38"
    assert hass.states.get("image.sam_latest_photo").attributes["photo_id"] == "photo3"
    assert hass.states.get("calendar.sam_day_rhythm").state == STATE_OFF


async def test_calendar_lists_moments(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.sam_day_rhythm", "start_date_time": "2026-10-07 00:00", "duration": {"days": 1}},
        blocking=True,
        return_response=True,
    )

    events = result["calendar.sam_day_rhythm"]["events"]
    assert [(e["summary"], e["start"], e["end"]) for e in events] == [
        ("09:00 (Fles)voeding", "2026-10-07T09:00:00+02:00", "2026-10-07T09:15:00+02:00"),
        ("10:10-10:48 Slapen", "2026-10-07T10:10:00+02:00", "2026-10-07T10:48:00+02:00"),
        ("12:00 (Fles)voeding", "2026-10-07T12:00:00+02:00", "2026-10-07T12:15:00+02:00"),
    ]


async def test_ongoing_nap_turns_calendar_on(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    mock_client.offsets[0] = _with(timeline_cards, _moment("slapen-bedje", "12:40 Slapen"))
    await _setup(hass, entry)

    assert hass.states.get("calendar.sam_day_rhythm").state == "on"
    assert hass.states.get("sensor.sam_last_sleep").attributes["sleeping"] is True


async def test_history_is_not_announced_but_new_entries_are(
    hass: HomeAssistant, entry, mock_client, timeline_cards
) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)
    assert events == []

    mock_client.offsets[0] = _with(
        timeline_cards, _moment("luier", "13:30 Luier", "Nat"), photo_ids=("photo5", "photo4")
    )
    await _refresh(hass, entry)

    assert [e.data["entry_type"] for e in events] == ["moment", "photos"]
    moment, photos = (e.data for e in events)
    assert moment["change"] == "new"
    assert moment["child_id"] == CHILD_ID
    assert moment["child_name"] == "Sam"
    assert moment["category"] == "diaper"
    assert moment["text"] == "13:30 Luier - Nat"
    assert moment["start"] == "2026-10-07T13:30:00+02:00"
    assert photos["count"] == 2
    assert [p["id"] for p in photos["photos"]] == ["photo5", "photo4"]
    assert photos["image_url"].endswith("IMG-3-medium.jpeg?Expires=1&Key-Pair-Id=TEST&Signature=test")
    assert hass.states.get("image.sam_latest_photo").attributes["photo_id"] == "photo5"

    # Nothing new: nothing fired.
    await _refresh(hass, entry)
    assert len(events) == 2


async def test_finished_nap_is_announced_as_update(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    mock_client.offsets[0] = _with(timeline_cards, _moment("slapen-bedje", "12:40 Slapen"))
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.offsets[0] = _with(timeline_cards, _moment("slapen-bedje", "12:40 - 13:25 Slapen"))
    await _refresh(hass, entry)

    (event,) = events
    assert event.data["change"] == "updated"
    assert event.data["duration_minutes"] == 45


async def test_seen_entries_survive_a_restart(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)

    mock_client.offsets[0] = _with(timeline_cards, _moment("luier", "13:30 Luier"))
    await _setup(hass, entry)

    assert [e.data["title"] for e in events] == ["13:30 Luier"]


async def test_empty_timeline_does_not_reset_seen_state(
    hass: HomeAssistant, entry, mock_client, timeline_cards
) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.offsets[0] = []
    await _refresh(hass, entry)
    mock_client.offsets[0] = timeline_cards
    await _refresh(hass, entry)

    assert events == []


async def test_portal_down_makes_entities_unavailable(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    mock_client.get_timeline.side_effect = OuderportaalConnectionError("down")
    await _refresh(hass, entry)

    assert hass.states.get("sensor.sam_last_activity").state == STATE_UNAVAILABLE


async def test_portal_down_at_startup_retries(hass: HomeAssistant, entry, mock_client) -> None:
    mock_client.get_children.side_effect = OuderportaalConnectionError("down")

    await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rejected_password_starts_reauth(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    mock_client.get_timeline.side_effect = OuderportaalAuthError("password changed")
    await _refresh(hass, entry)

    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
