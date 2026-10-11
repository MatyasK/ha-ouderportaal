"""Shared fixtures."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CHILD_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
CLIENT = "custom_components.ouderportaal.api.OuderportaalClient"
PHOTO_BYTES = b"\xff\xd8\xff\xe0fake jpeg"
TZ = ZoneInfo("Europe/Amsterdam")


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def timeline_cards() -> list[dict[str, Any]]:
    return load_fixture("timeline.json")


@pytest.fixture
def children_payload() -> dict[str, Any]:
    return load_fixture("children.json")


def schedule_day(day: dict[str, Any]) -> date:
    # The Agenda dates a day with the time of the request on that day.
    return datetime.fromtimestamp(day["date"] / 1000, TZ).date()


@pytest.fixture
def mock_client(timeline_cards, children_payload) -> Iterator[SimpleNamespace]:
    """Replace the network calls of the API client; everything above it runs for real.

    Set `mock_client.offsets[n]` to the cards the timeline returns from card offset n; other offsets are empty.
    Set `mock_client.schedule` to the Agenda's days; like the portal, a request returns the ones in its range.
    """
    offsets: dict[int, list[dict[str, Any]]] = {0: timeline_cards}

    async def get_timeline(offset: int = 0) -> list[dict[str, Any]]:
        return copy.deepcopy(offsets.get(offset, []))

    async def get_schedule(start: date, end: date) -> list[dict[str, Any]]:
        return copy.deepcopy([day for day in mocks.schedule if start <= schedule_day(day) <= end])

    mocks = SimpleNamespace(
        login=AsyncMock(),
        get_children=AsyncMock(return_value=children_payload["payload"]["activeChildren"]),
        get_timeline=AsyncMock(side_effect=get_timeline),
        get_authorizations=AsyncMock(
            return_value=set(load_fixture("authorizations.json")["payload"]["systemFunctions"])
        ),
        get_messages=AsyncMock(return_value=load_fixture("messages.json")),
        get_news=AsyncMock(return_value=load_fixture("news.json")["payload"]["newsItems"]),
        get_newsletters=AsyncMock(return_value=load_fixture("newsletters.json")["payload"]["newsletterItems"]),
        get_notifications=AsyncMock(
            return_value={"nrOfNewMessages": 1, "nrOfNewNewsItems": 1, "nrOfNewNewsletters": None}
        ),
        get_schedule=AsyncMock(side_effect=get_schedule),
    )
    with (
        patch.multiple(CLIENT, **vars(mocks)),
        patch("custom_components.ouderportaal.coordinator.BACKFILL_PAGE_DELAY_S", 0),
    ):
        mocks.offsets = offsets
        mocks.schedule = load_fixture("schedule.json")["payload"]["days"]
        yield mocks


@pytest.fixture
def photo_cdn(aioclient_mock):
    """The portal's photo CDN: every photo URL returns a small fake JPEG."""
    aioclient_mock.get(re.compile(r"^https://resource\.kidskonnect\.cloud/"), content=PHOTO_BYTES)
    return aioclient_mock


@pytest.fixture
def media_dir(hass, tmp_path) -> Path:
    hass.config.media_dirs = {"local": str(tmp_path)}
    return tmp_path / "ouderportaal"
