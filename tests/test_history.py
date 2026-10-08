"""History import, saved photos, and the timeline websocket API."""

from __future__ import annotations

import copy
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ouderportaal.api import OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN, EVENT_NEW_ENTRY

from .conftest import CHILD_ID, PHOTO_BYTES

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
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

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
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

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
    assert [d["date"] for d in child["days"]] == ["2026-10-07", "2026-10-06"]
    today = child["days"][0]
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
