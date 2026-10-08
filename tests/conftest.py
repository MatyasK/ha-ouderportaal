"""Shared fixtures."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CHILD_ID = "c0ffee00c0ffee00c0ffee00c0ffee00"
CLIENT = "custom_components.ouderportaal.api.OuderportaalClient"
PHOTO_BYTES = b"\xff\xd8\xff\xe0fake jpeg"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def timeline_cards() -> list[dict[str, Any]]:
    return load_fixture("timeline.json")


@pytest.fixture
def children_payload() -> dict[str, Any]:
    return load_fixture("children.json")


@pytest.fixture
def mock_client(timeline_cards, children_payload) -> Iterator[SimpleNamespace]:
    """Replace the network calls of the API client; everything above it runs for real.

    Set `mock_client.offsets[n]` to the cards the timeline returns from card offset n; other offsets are empty.
    """
    offsets: dict[int, list[dict[str, Any]]] = {0: timeline_cards}

    async def get_timeline(offset: int = 0) -> list[dict[str, Any]]:
        return copy.deepcopy(offsets.get(offset, []))

    mocks = SimpleNamespace(
        login=AsyncMock(),
        get_children=AsyncMock(return_value=children_payload["payload"]["activeChildren"]),
        get_timeline=AsyncMock(side_effect=get_timeline),
    )
    with (
        patch.multiple(CLIENT, **vars(mocks)),
        patch("custom_components.ouderportaal.coordinator.BACKFILL_PAGE_DELAY_S", 0),
    ):
        mocks.offsets = offsets
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
