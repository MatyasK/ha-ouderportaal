"""History import, saved photos, and the timeline websocket API."""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.archive import PhotoArchive
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN, EVENT_NEW_ENTRY
from custom_components.ouderportaal.timeline import Photo, Timeline

from .conftest import CHILD_ID, PHOTO_BYTES, load_fixture

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

TZ = ZoneInfo("Europe/Amsterdam")
IMG_3 = "https://resource.kidskonnect.cloud/test/IMG-3.jpeg?Expires=1&Key-Pair-Id=TEST&Signature=test"
NOW = "2026-10-07T11:00:00+00:00"  # 13:00 in Amsterdam; 30 days back is 2026-09-07.


def _day_cards(cards: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """The fixture's journal and photo cards, moved to another day with their own photo ids."""
    cards = copy.deepcopy(cards)
    millis = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp() * 1000)
    for card in cards:
        card["date"] = millis
        for photo in card.get("photos") or []:
            photo["id"] = f"{photo['id']}-{day.isoformat()}"
            photo["date"] = millis
    return cards


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


async def _restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _setup(hass, entry)


@pytest.fixture
def history_pages(mock_client, timeline_cards) -> None:
    """Offset 0 holds 2 cards, so older days follow at offsets 2 and 6; offset 6 reaches past the 30 day window."""
    mock_client.offsets[2] = _day_cards(timeline_cards, date(2026, 10, 6)) + _day_cards(
        timeline_cards, date(2026, 9, 9)
    )
    mock_client.offsets[6] = _day_cards(timeline_cards, date(2026, 9, 8)) + _day_cards(timeline_cards, date(2026, 9, 6))
    mock_client.offsets[10] = _day_cards(timeline_cards, date(2026, 8, 1))


@pytest.mark.usefixtures("history_pages")
async def test_imports_last_30_days_once_without_announcing(hass: HomeAssistant, entry, mock_client) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)

    await _setup(hass, entry)

    history = entry.runtime_data.history
    assert sorted({m.day.isoformat() for m in history.moments}) == [
        "2026-09-08",
        "2026-09-09",
        "2026-10-06",
        "2026-10-07",
    ]
    assert len(history.photos) == 4 * 3
    assert events == []
    # First poll, then the import pages by card offset and stops once it is past the window.
    assert [c.args[-1] for c in mock_client.get_timeline.await_args_list] == [0, 0, 2, 6]

    await _restart(hass, entry)
    assert [c.args[-1] for c in mock_client.get_timeline.await_args_list] == [0, 0, 2, 6, 0]
    assert len(entry.runtime_data.history.photos) == 12


@pytest.mark.usefixtures("history_pages")
async def test_photos_are_saved_in_the_media_folder(hass: HomeAssistant, entry, mock_client, media_dir) -> None:
    await _setup(hass, entry)

    saved = sorted(p.relative_to(media_dir).as_posix() for p in media_dir.rglob("*") if p.is_file())
    assert len(saved) == 12
    assert "2026-10-07/photo3.jpeg" in saved
    assert "2026-09-08/photo1-2026-09-08.jpeg" in saved
    assert (media_dir / "2026-10-07" / "photo3.jpeg").read_bytes() == PHOTO_BYTES


async def test_failed_import_is_retried_after_restart(hass: HomeAssistant, entry, mock_client, timeline_cards) -> None:
    serve_pages = mock_client.get_timeline.side_effect
    mock_client.offsets[2] = _day_cards(timeline_cards, date(2026, 10, 1))

    async def older_cards_down(offset: int = 0):
        if offset == 2:
            raise OuderportaalConnectionError("down")
        return await serve_pages(offset)

    mock_client.get_timeline.side_effect = older_cards_down
    await _setup(hass, entry)
    assert "2026-10-01" not in {m.day.isoformat() for m in entry.runtime_data.history.moments}

    mock_client.get_timeline.side_effect = serve_pages
    await _restart(hass, entry)

    assert "2026-10-01" in {m.day.isoformat() for m in entry.runtime_data.history.moments}


async def test_unwritable_media_folder_only_stops_saving_photos(
    hass: HomeAssistant, entry, mock_client, media_dir, caplog
) -> None:
    # The photos' folder is taken by a file, like a media folder Home Assistant cannot write to.
    media_dir.write_text("not a folder")
    caplog.set_level(logging.INFO)

    await _setup(hass, entry)
    await _refresh(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.last_update_success
    assert hass.states.get("sensor.sam_last_activity").state == "12:00 (Fles)voeding"
    assert hass.states.get("image.sam_latest_photo").attributes["photo_id"] == "photo3"
    # Warned once, not on every poll.
    assert caplog.text.count("Could not save photos") == 1

    media_dir.unlink()
    await _refresh(hass, entry)

    assert (media_dir / "2026-10-07" / "photo3.jpeg").read_bytes() == PHOTO_BYTES
    assert "Saving photos works again" in caplog.text


@pytest.mark.parametrize("rejected", ["get_timeline", "get_messages"])
async def test_rejected_password_during_import_starts_reauth(
    hass: HomeAssistant, entry, mock_client, timeline_cards, rejected: str
) -> None:
    first_poll = timeline_cards if rejected == "get_timeline" else load_fixture("messages.json")
    getattr(mock_client, rejected).side_effect = [first_poll, OuderportaalAuthError("password changed")]

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress()] == [SOURCE_REAUTH]


async def test_unchanged_poll_does_not_serialize_the_history(hass: HomeAssistant, entry, mock_client) -> None:
    await _setup(hass, entry)

    with patch.object(Timeline, "to_dict", autospec=True, side_effect=Timeline.to_dict) as to_dict:
        await _refresh(hass, entry)

    to_dict.assert_not_called()


async def test_history_is_saved_shortly_after_a_change(
    hass: HomeAssistant, entry, mock_client, timeline_cards, hass_storage, freezer
) -> None:
    await _setup(hass, entry)
    cards = copy.deepcopy(timeline_cards)
    cards[1]["photos"].insert(0, cards[1]["photos"][0] | {"id": "photo4"})
    mock_client.offsets[0] = cards

    await _refresh(hass, entry)
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    stored = hass_storage[f"ouderportaal.{entry.entry_id}.history"]["data"]
    assert "photo4" in {p["id"] for p in stored["timeline"]["photos"]}


async def test_expired_photo_url_is_retried_on_next_poll(
    hass: HomeAssistant, entry, photo_cdn, media_dir, mock_client
) -> None:
    photo_cdn.clear_requests()
    photo_cdn.get(IMG_3, status=403)
    photo_cdn.get(re.compile(r"^https://resource\.kidskonnect\.cloud/"), content=PHOTO_BYTES)
    await _setup(hass, entry)
    assert not (media_dir / "2026-10-07" / "photo3.jpeg").exists()

    photo_cdn.clear_requests()
    photo_cdn.get(re.compile(r"^https://resource\.kidskonnect\.cloud/"), content=PHOTO_BYTES)
    await _refresh(hass, entry)

    assert (media_dir / "2026-10-07" / "photo3.jpeg").read_bytes() == PHOTO_BYTES


async def test_latest_photo_is_served_from_saved_copy(
    hass: HomeAssistant, entry, mock_client, hass_client, photo_cdn
) -> None:
    await _setup(hass, entry)
    requests_before = photo_cdn.call_count

    client = await hass_client()
    resp = await client.get("/api/image_proxy/image.sam_latest_photo")

    assert resp.status == 200
    assert await resp.read() == PHOTO_BYTES
    assert resp.content_type == "image/jpeg"
    assert photo_cdn.call_count == requests_before
    state = hass.states.get("image.sam_latest_photo")
    assert state.attributes["media_url"] == "/media/local/ouderportaal/2026-10-07/photo3.jpeg"


async def test_new_photos_event_points_at_saved_copy(
    hass: HomeAssistant, entry, mock_client, timeline_cards, media_dir
) -> None:
    events = async_capture_events(hass, EVENT_NEW_ENTRY)
    await _setup(hass, entry)

    cards = copy.deepcopy(timeline_cards)
    cards[1]["photos"].insert(0, cards[1]["photos"][0] | {"id": "photo4"})
    mock_client.offsets[0] = cards
    await _refresh(hass, entry)

    (event,) = events
    assert event.data["media_url"] == "/media/local/ouderportaal/2026-10-07/photo4.jpeg"
    assert event.data["photos"][0]["media_content_id"] == (
        "media-source://media_source/local/ouderportaal/2026-10-07/photo4.jpeg"
    )
    # Saved before the event fired, so a notification can load it straight away.
    assert (media_dir / "2026-10-07" / "photo4.jpeg").exists()


@pytest.mark.usefixtures("history_pages")
async def test_websocket_returns_days_newest_first(hass: HomeAssistant, entry, mock_client, hass_ws_client) -> None:
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "ouderportaal/timeline", "days": 7})
    result = (await ws.receive_json())["result"]

    (child,) = result["children"]
    assert child["id"] == CHILD_ID
    assert child["name"] == "Sam"
    # 2026-10-05 and 2026-10-02 only have news and newsletters, which are for the whole family.
    assert [d["date"] for d in child["days"]] == ["2026-10-07", "2026-10-06", "2026-10-05", "2026-10-02"]
    today = child["days"][0]
    assert [m["subject"] for m in today["messages"]] == ["Uitje naar de kinderboerderij"]
    assert today["messages"][0]["key"] == "message-msg-trip"
    assert today["messages"][0]["author"] == "Tommie"
    assert today["messages"][0]["child_names"] == ["Sam"]
    assert today["messages"][0]["url"] == "https://mijnopvang.ouderportaal.nl/parent/#/tabs/mailbox"
    assert [(m["message_type"], m["subject"]) for m in child["days"][2]["messages"]] == [
        ("newsletter", "Samen voor de beste kwaliteit - Deel uw ervaring!"),
        ("news", "Sluitingsdag 24 oktober"),
    ]
    assert [m["message_type"] for m in child["days"][3]["messages"]] == ["service_message"]
    assert [m["title"] for m in today["moments"]] == [
        "09:00 (Fles)voeding",
        "10:10-10:48 Slapen",
        "12:00 (Fles)voeding",
    ]
    assert today["moments"][0]["amount_ml"] == 130
    assert today["photos"][0] == {
        "id": "photo3",
        "description": "In this photo is Sam",
        "media_type": "photo",
        "saved": True,
        "url": "/media/local/ouderportaal/2026-10-07/photo3.jpeg",
        "media_content_id": "media-source://media_source/local/ouderportaal/2026-10-07/photo3.jpeg",
    }


@pytest.mark.usefixtures("history_pages")
async def test_websocket_only_looks_up_the_photos_it_returns(
    hass: HomeAssistant, entry, mock_client, hass_ws_client
) -> None:
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)
    looked_up: list[str] = []
    saved = PhotoArchive.async_saved

    async def spy(archive: PhotoArchive, photos: Iterable[Photo]) -> set[str]:
        photos = list(photos)
        looked_up.extend(p.id for p in photos)
        return await saved(archive, photos)

    with patch.object(PhotoArchive, "async_saved", spy):
        await ws.send_json({"id": 1, "type": "ouderportaal/timeline", "days": 1})
        today = (await ws.receive_json())["result"]["children"][0]["days"][0]
        await ws.send_json({"id": 2, "type": "ouderportaal/timeline", "child_id": "someone-else"})
        await ws.receive_json()

    assert sorted(looked_up) == ["photo1", "photo2", "photo3"]
    assert all(p["saved"] for p in today["photos"])


async def test_websocket_filters_on_child(hass: HomeAssistant, entry, mock_client, hass_ws_client) -> None:
    await _setup(hass, entry)
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "ouderportaal/timeline", "child_id": "someone-else"})

    assert (await ws.receive_json())["result"] == {"children": []}


@pytest.mark.usefixtures("history_pages")
async def test_import_reruns_for_installs_with_the_old_paging(
    hass: HomeAssistant, entry, mock_client, hass_storage
) -> None:
    """Version 0.2 stored 'backfilled: true' after paging wrongly and importing nothing."""
    hass_storage[f"ouderportaal.{entry.entry_id}.history"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"ouderportaal.{entry.entry_id}.history",
        "data": {"backfilled": True, "timeline": {"moments": [], "photos": []}},
    }

    await _setup(hass, entry)

    assert "2026-09-09" in {m.day.isoformat() for m in entry.runtime_data.history.moments}
