"""Diagnostics: what helps to find a problem, without the account, the children or what the daycare wrote.

Children are numbered instead of named, and the history is summed up in counts: no names, texts, photos or ids.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import CONF_PORTAL
from .coordinator import OuderportaalConfigEntry, OuderportaalCoordinator

# The portal's name tells which daycare the children go to; the entry's title and unique id contain it too.
TO_REDACT = {CONF_PORTAL, CONF_USERNAME, CONF_PASSWORD, "title", "unique_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> dict[str, Any]:
    portal = entry.data[CONF_PORTAL]
    diagnostics: dict[str, Any] = {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "state": entry.state.value,
        # Why setting up failed, e.g. the portal could not be reached.
        "reason": _without_portal(entry.reason, portal),
    }
    if entry.state is ConfigEntryState.LOADED:
        diagnostics |= await _loaded(entry.runtime_data, portal)
    return diagnostics


async def _loaded(coordinator: OuderportaalCoordinator, portal: str) -> dict[str, Any]:
    history = coordinator.history
    sessions = coordinator.schedule.data if coordinator.schedule and coordinator.schedule.data else []
    saved = await coordinator.archive.async_saved(history.photos)
    days = [m.day for m in history.moments] + [p.day for p in history.photos] + [m.day for m in history.messages]
    icons = Counter((m.icon, m.category) for m in history.moments)
    return {
        "authorizations": sorted(coordinator.authorizations),
        "timeline": _status(coordinator, portal) | {"messages_failing": coordinator.messages_failing},
        "schedule": _status(coordinator.schedule, portal) if coordinator.schedule else None,
        "history_imported": coordinator.history_imported,
        "unread_counts": coordinator.unread_counts,
        # In the portal's order.
        "children": [
            {
                "child": f"child {number}",
                "moments": dict(Counter(m.category for m in history.moments if m.child_id == child_id)),
                "photos": sum(child_id in p.child_ids for p in history.photos),
                "sessions": dict(Counter(s.summary for s in sessions if s.child_id == child_id)),
            }
            for number, child_id in enumerate(coordinator.children, 1)
        ],
        "history": {
            "first_day": min(days).isoformat() if days else None,
            "last_day": max(days).isoformat() if days else None,
            "moments": len(history.moments),
            "moments_without_time": sum(m.start is None for m in history.moments),
            # The daycare picks an icon per moment; one that is not known yet shows up as 'activity'.
            "icons": [
                {"icon": icon, "category": category, "count": count}
                for (icon, category), count in sorted(icons.items())
            ],
            "photos": len(history.photos),
            "photos_saved": len(saved),
            "photo_media_types": dict(Counter(p.media_type for p in history.photos)),
            "saving_photos_fails": coordinator.archive.write_failing,
            "messages": dict(Counter(m.message_type for m in history.messages)),
        },
    }


def _status(coordinator: DataUpdateCoordinator[Any], portal: str) -> dict[str, Any]:
    error = coordinator.last_exception
    return {
        "last_update_success": coordinator.last_update_success,
        "update_interval": str(coordinator.update_interval),
        "last_exception": _without_portal(f"{type(error).__name__}: {error}", portal) if error else None,
    }


def _without_portal(text: str | None, portal: str) -> str | None:
    """An error can name the portal's address, such as 'Cannot connect to host <portal>.ouderportaal.nl'."""
    return text.replace(portal, REDACTED) if text else text
