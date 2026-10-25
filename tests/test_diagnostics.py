"""Diagnostics: what helps to find a problem, without the account, the children or what the daycare wrote."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ouderportaal.api import OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN

from .conftest import CHILD_ID, load_fixture

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

NOW = "2026-10-07T11:00:00+00:00"  # 13:00 in Amsterdam
GROUP = {"id": "group-1", "name": "Rupsjes", "location": {"id": "loc-1", "name": "Kinderopvang De Test"}}


@pytest.fixture(autouse=True)
def _quiet_access_log(caplog) -> None:
    """aiohttp's access log needs the UTC offset, which the frozen clock's local time does not have."""
    caplog.set_level(logging.WARNING, logger="aiohttp.access")


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
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _download(hass: HomeAssistant, hass_client, entry: MockConfigEntry) -> dict[str, Any]:
    """What 'Download diagnostics' on the integration's page gives."""
    assert await async_setup_component(hass, "diagnostics", {})
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == HTTPStatus.OK
    return await response.json()


def _personal(entry: MockConfigEntry) -> set[str]:
    """Everything the integration knows that tells who the family is, or what the daycare wrote them."""
    coordinator = entry.runtime_data
    history = coordinator.history
    values = {*entry.data.values(), entry.title, entry.unique_id}
    for child in coordinator.children.values():
        values |= {child.id, child.first_name, child.full_name}
    for moment in history.moments:
        values |= {moment.description, moment.remarks}
    for photo in history.photos:
        values |= {photo.id, photo.description}
    for message in history.messages:
        values |= {message.id, message.subject, message.preview, message.author}
    for session in coordinator.schedule.data:
        values |= {session.description, session.location}
    return {value for value in values if value}


async def test_diagnostics_leave_out_the_family(hass: HomeAssistant, entry, mock_client, hass_client) -> None:
    for day in mock_client.schedule:
        for planning in day["children"]:
            for combined in planning["combinedPlanningParts"]:
                for part in combined["planningParts"]:
                    part["childGroup"] = GROUP
    await _setup(hass, entry)

    personal = _personal(entry)
    # Everything is in there to leave out: the account, the child, the daycare's texts, its group and location.
    assert {"Sam", CHILD_ID, "Tommie", "parent@example.com", "mijnopvang", "Kinderopvang De Test"} <= personal
    text = json.dumps(await _download(hass, hass_client, entry))
    assert [value for value in personal if value in text] == []


async def test_diagnostics_show_what_helps(hass: HomeAssistant, entry, mock_client, hass_client) -> None:
    await _setup(hass, entry)

    data = (await _download(hass, hass_client, entry))["data"]

    assert data["entry"]["data"] == {CONF_PORTAL: REDACTED, CONF_USERNAME: REDACTED, CONF_PASSWORD: REDACTED}
    assert data["entry"]["title"] == REDACTED
    assert data["entry"]["unique_id"] == REDACTED
    assert data["state"] == "loaded"
    assert data["reason"] is None
    assert data["authorizations"] == sorted(load_fixture("authorizations.json")["payload"]["systemFunctions"])
    ok = {"last_update_success": True, "last_exception": None}
    assert data["timeline"] == ok | {"update_interval": "0:05:00", "messages_failing": False}
    assert data["schedule"] == ok | {"update_interval": "0:30:00"}
    assert data["history_imported"] is True
    assert data["unread_counts"] == {"messages": 1, "news": 1, "newsletters": 0}
    assert data["children"] == [
        {"child": "child 1", "moments": {"bottle": 2, "sleep": 1}, "photos": 3, "sessions": {"Daycare": 8}}
    ]
    assert data["history"] == {
        "first_day": "2026-10-02",
        "last_day": "2026-10-07",
        "moments": 3,
        "moments_without_time": 0,
        "icons": [
            {"icon": "flesje", "category": "bottle", "count": 2},
            {"icon": "slapen-bedje", "category": "sleep", "count": 1},
        ],
        "photos": 3,
        "photos_saved": 3,
        "photo_media_types": {"photo": 3},
        "saving_photos_fails": False,
        "messages": {"message": 2, "news": 1, "newsletter": 1, "service_message": 1},
    }


async def test_diagnostics_show_why_a_poll_failed(hass: HomeAssistant, entry, mock_client, hass_client) -> None:
    await _setup(hass, entry)
    mock_client.get_timeline.side_effect = OuderportaalConnectionError(
        "/timeline/cards/v2/0 failed: Cannot connect to host mijnopvang.ouderportaal.nl:443"
    )
    await entry.runtime_data.async_refresh()

    data = (await _download(hass, hass_client, entry))["data"]

    assert data["timeline"]["last_update_success"] is False
    assert data["timeline"]["last_exception"] == (
        f"UpdateFailed: /timeline/cards/v2/0 failed: Cannot connect to host {REDACTED}.ouderportaal.nl:443"
    )


async def test_diagnostics_of_an_entry_that_could_not_start(
    hass: HomeAssistant, entry, mock_client, hass_client
) -> None:
    mock_client.get_children.side_effect = OuderportaalConnectionError(
        "/children/ failed: Cannot connect to host mijnopvang.ouderportaal.nl:443"
    )
    await _setup(hass, entry)

    data = (await _download(hass, hass_client, entry))["data"]

    assert data["state"] == "setup_retry"
    assert data["reason"] == f"/children/ failed: Cannot connect to host {REDACTED}.ouderportaal.nl:443"
    assert "timeline" not in data
