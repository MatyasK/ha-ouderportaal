"""Websocket API for a timeline card: `{"type": "ouderportaal/timeline"}`.

Returns per child the days (newest first) with their moments and photos. Photo URLs point at the saved
copies under /media/local/...; a card signs them with `auth/sign_path` before using them in an <img>.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import OuderportaalCoordinator
from .timeline import Moment, Photo


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_timeline)


def _moment(moment: Moment) -> dict[str, Any]:
    return moment.to_dict() | {
        "key": moment.key,
        "title": moment.title,
        "text": moment.text,
        "duration_minutes": moment.duration_minutes,
    }


def _photo(coordinator: OuderportaalCoordinator, photo: Photo, saved: set[str]) -> dict[str, Any]:
    return {
        "id": photo.id,
        "description": photo.description,
        "media_type": photo.media_type,
        "saved": photo.id in saved,
        "url": coordinator.archive.media_url(photo),
        "media_content_id": coordinator.archive.media_content_id(photo),
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/timeline",
        vol.Optional("child_id"): str,
        vol.Optional("days", default=30): vol.All(int, vol.Range(min=1, max=3660)),
    }
)
@websocket_api.async_response
async def ws_timeline(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    since = dt_util.now().date() - timedelta(days=msg["days"] - 1)
    children = []
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator: OuderportaalCoordinator = entry.runtime_data
        history = coordinator.history
        saved = await coordinator.archive.async_saved(history.photos)
        for child in coordinator.children.values():
            if msg.get("child_id") not in (None, child.id):
                continue
            days: dict[str, dict[str, Any]] = {}

            def day(iso: str, days: dict[str, dict[str, Any]] = days) -> dict[str, Any]:
                return days.setdefault(iso, {"date": iso, "moments": [], "photos": []})

            for moment in history.moments:
                if moment.child_id == child.id and moment.day >= since:
                    day(moment.day.isoformat())["moments"].append(_moment(moment))
            for photo in history.photos:
                if child.id in photo.child_ids and photo.day >= since:
                    day(photo.day.isoformat())["photos"].append(_photo(coordinator, photo, saved))
            children.append(
                {
                    "id": child.id,
                    "name": child.first_name,
                    "days": [days[iso] for iso in sorted(days, reverse=True)],
                }
            )
    connection.send_result(msg["id"], {"children": children})
