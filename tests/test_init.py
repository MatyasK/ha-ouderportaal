"""Setting up the integration: entities, events, persistence and failure handling."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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
async def entry(hass: HomeAssistant, freezer, archive_dir, photo_cdn) -> MockConfigEntry:
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


async def _day_events(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The day rhythm calendar's events on the fixture's day."""
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.sam_day_rhythm", "start_date_time": "2026-10-07 00:00", "duration": {"days": 1}},
        blocking=True,
        return_response=True,
    )
    return result["calendar.sam_day_rhythm"]["events"]


async def test_calendar_lists_moments(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    events = await _day_events(hass)
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


async def test_extended_note_is_announced_as_update(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    mock_client.offsets[0] = copy.deepcopy(timeline_cards)
    mock_client.offsets[0][0]["journal"]["journalContent"] = "<p>Lekker gespeeld</p>"
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    mock_client.offsets[0] = copy.deepcopy(mock_client.offsets[0])
    mock_client.offsets[0][0]["journal"]["journalContent"] = "<p>Lekker gespeeld</p><p>Tot morgen!</p>"
    await _refresh(hass, entry)
    mock_client.offsets[0][0]["journal"]["journalContent"] = "<p>Lekker gespeeld</p><p>Tot morgen!</p><p>Juf</p>"
    await _refresh(hass, entry)

    assert [(e.data["category"], e.data["change"]) for e in events] == [("note", "updated"), ("note", "updated")]
    notes = [e for e in await _day_events(hass) if e["summary"] == "Note from Juf"]
    assert [n["description"] for n in notes] == ["Lekker gespeeld\nTot morgen!\nJuf"]


async def test_corrected_day_rhythm_replaces_the_old_entries(
    hass: HomeAssistant, entry, mock_client, timeline_cards
) -> None:
    mock_client.offsets[0] = _with(timeline_cards, _moment("luier", "13:30 Luier"), _moment("luier", "13:40 Luier"))
    await _setup(hass, entry)

    # The daycare moves the first diaper to 13:20 and removes the second.
    mock_client.offsets[0] = _with(timeline_cards, _moment("luier", "13:20 Luier"))
    await _refresh(hass, entry)

    assert [e["summary"] for e in await _day_events(hass)] == [
        "09:00 (Fles)voeding",
        "10:10-10:48 Slapen",
        "12:00 (Fles)voeding",
        "13:20 Luier",
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, entry)
    assert [m.title for m in entry.runtime_data.history.moments][-1] == "13:20 Luier"
    assert len(entry.runtime_data.history.moments) == 4


async def test_unusable_card_does_not_stop_the_update(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    broken = copy.deepcopy(timeline_cards[0]) | {"date": None}
    impossible = _with(timeline_cards, _moment("slapen-bedje", "12:40 - 24:00 Slapen"))[0]
    impossible["date"] -= 24 * 3600 * 1000
    mock_client.offsets[0] = [broken, impossible, *timeline_cards]

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.sam_last_activity").state == "12:00 (Fles)voeding"
    assert hass.states.get("sensor.sam_milk_today").state == "270.0"


async def test_existing_second_account_keeps_working(hass: HomeAssistant, entry, mock_client, device_registry) -> None:
    """Setup no longer allows a second account on a portal, but installs from before that may have both parents'."""
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mijnopvang:other-parent@example.com",
        title="mijnopvang.ouderportaal.nl",
        data={CONF_PORTAL: "mijnopvang", CONF_USERNAME: "other-parent@example.com", CONF_PASSWORD: "secret"},
    )
    other.add_to_hass(hass)

    # Sets up both accounts.
    await _setup(hass, entry)

    assert other.state is ConfigEntryState.LOADED
    for entity_id in ("sensor.sam_last_activity", "sensor.sam_last_activity_2"):
        assert hass.states.get(entity_id).state == "12:00 (Fles)voeding"
    for entity_id in ("calendar.sam_day_rhythm", "calendar.sam_schedule", "image.sam_latest_photo"):
        assert hass.states.get(f"{entity_id}_2") is not None
    # Each account has its own devices.
    for config_entry in (entry, other):
        devices = dr.async_entries_for_config_entry(device_registry, config_entry.entry_id)
        assert sorted(d.name for d in devices) == ["Ouderportaal mailbox", "Sam"]
        assert all(d.config_entry_id == config_entry.entry_id for d in devices)


async def test_upgrade_keeps_entities_and_device(
    hass: HomeAssistant, entry, mock_client, entity_registry, device_registry
) -> None:
    """Before minor version 2, the ids of a child's device and entities did not include the account."""
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, CHILD_ID)}, name="Sam"
    )
    for domain, key in (("sensor", "last_activity"), ("calendar", "schedule"), ("image", "latest_photo")):
        entity_registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{CHILD_ID}_{key}",
            config_entry=entry,
            device_id=device.id,
            suggested_object_id=f"sam_{key}",
        )
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_unread_messages",
        config_entry=entry,
        suggested_object_id="ouderportaal_mailbox_unread_messages",
    )

    await _setup(hass, entry)

    assert (entry.version, entry.minor_version) == (1, 2)
    for entity_id, key in (
        ("sensor.sam_last_activity", "last_activity"),
        ("calendar.sam_schedule", "schedule"),
        ("image.sam_latest_photo", "latest_photo"),
    ):
        assert entity_registry.async_get(entity_id).unique_id == f"{entry.entry_id}_{CHILD_ID}_{key}"
        assert entity_registry.async_get(entity_id).device_id == device.id
        assert hass.states.get(entity_id) is not None
        assert hass.states.get(f"{entity_id}_2") is None
    assert entity_registry.async_get("sensor.ouderportaal_mailbox_unread_messages").unique_id == (
        f"{entry.entry_id}_unread_messages"
    )
    assert hass.states.get("sensor.sam_last_activity").state == "12:00 (Fles)voeding"
    assert device_registry.async_get(device.id).identifiers == {(DOMAIN, f"{entry.entry_id}_{CHILD_ID}")}


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
