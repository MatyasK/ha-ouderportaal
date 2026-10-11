"""Check the client against your real portal, outside Home Assistant.

    uv run --no-project --with aiohttp python scripts/try_portal.py mijnopvang you@example.com

Asks for your password, logs in, forces a token refresh, and prints the latest moments, the Messages page and the
schedule. Nothing is stored.
"""

from __future__ import annotations

import asyncio
import getpass
import importlib.util
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "ouderportaal"


def _load(name: str) -> ModuleType:
    """Load one of the HA-free modules by path; the package's __init__ needs Home Assistant.

    Not via sys.path: the package's calendar.py would shadow the standard library.
    """
    spec = importlib.util.spec_from_file_location(f"ouderportaal_{name}", PACKAGE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


OuderportaalClient = _load("api").OuderportaalClient
_timeline = _load("timeline")
parse_timeline = _timeline.parse_timeline
parse_messages = _timeline.parse_messages
parse_news = _timeline.parse_news
parse_newsletters = _timeline.parse_newsletters
_schedule = _load("schedule")
parse_schedule = _schedule.parse_schedule
schedule_ranges = _schedule.schedule_ranges
_const = _load("const")


async def _raw_get(client: Any, path: str) -> tuple[int, Any]:
    """GET a restservices-parent path without unwrapping the answer, so the portal's own messages show."""
    token = await client._ensure_token()
    headers = client._base_headers() | {"Authorization": f"Bearer {token}"}
    async with client._session.get(f"{client.base_url}/restservices-parent{path}", headers=headers) as resp:
        return resp.status, await resp.json(content_type=None) if resp.status == 200 else None


async def main(portal: str, username: str) -> None:
    password = getpass.getpass(f"Password for {username}: ")
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        client = OuderportaalClient(session, portal, username, password)

        await client.login()
        print(f"1. Login OK, token valid for {client._expires_at - time.time():.0f}s")
        print(f"   Refresh cookie received: {client._refresh_cookie is not None}")

        children = await client.get_children()
        print(f"2. Children: {', '.join(c.get('firstName', '?') for c in children)}")

        refreshed = await client._refresh()
        print(f"3. Token refresh via cookie: {'OK' if refreshed else 'FAILED (the integration would log in again)'}")

        cards = await client.get_timeline(0)
        timeline = parse_timeline(cards, ZoneInfo("Europe/Amsterdam"))
        print(f"4. Timeline page 0: {len(cards)} cards, {len(timeline.moments)} moments, {len(timeline.photos)} photos")
        for moment in timeline.moments:
            print(f"   {moment.day} [{moment.category:>8}] {moment.text}")
        unknown = sorted({m.icon for m in timeline.moments if m.category == "activity"})
        if unknown:
            print(f"   Icons mapped to 'activity': {', '.join(unknown)}")

        mailbox = sorted(a for a in await client.get_authorizations() if "NEWS" in a or "LOGBOOK" in a)
        print(f"5. Messages page, last 30 days (permissions: {', '.join(mailbox)})")
        today = date.today()
        start, end = today - timedelta(days=30), today + timedelta(days=1)
        messages = []
        for name, fetch, parse in (
            ("conversations", client.get_messages, parse_messages),
            ("news", client.get_news, parse_news),
            ("newsletters", client.get_newsletters, parse_newsletters),
        ):
            items = await fetch(start, end)
            understood = parse(items, ZoneInfo("Europe/Amsterdam"))
            print(f"   {name}: {len(items)} items, {len(understood)} understood")
            if items:
                print(f"     Fields: {', '.join(sorted(items[0]))}")
            messages += understood
        for message in sorted(messages, key=lambda m: m.sent, reverse=True):
            unread = "*" if message.unread else " "
            print(f"   {message.sent:%Y-%m-%d %H:%M} {unread} [{message.message_type:>15}] {message.subject}")
        counts = await client.get_notifications()
        print(f"   Unread according to the portal: {counts}")

        print("6. Schedule, as the integration reads it: a month back to three months ahead, a month per request")
        days = []
        for start, end in schedule_ranges(
            today, _const.SCHEDULE_PAST_DAYS, _const.SCHEDULE_FUTURE_DAYS, _const.SCHEDULE_REQUEST_DAYS
        ):
            status, answer = await _raw_get(client, f"/calendar/{start:%Y%m%d}/until/{end:%Y%m%d}")
            answered = (answer or {}).get("payload") or {}
            # The portal's own messages say, for example, how far ahead it shows the planning.
            print(
                f"   {start} until {end}: HTTP {status}, result={(answer or {}).get('result')}, "
                f"{len(answered.get('days') or [])} days, messages={(answer or {}).get('messages')}"
            )
            days += answered.get("days") or []
        parts = [
            part
            for day in days
            for planning in day.get("children") or []
            for combined in planning.get("combinedPlanningParts") or []
            for part in combined.get("planningParts") or []
        ]
        sessions = parse_schedule(days, ZoneInfo("Europe/Amsterdam"))
        print(f"   {len(parts)} planned parts, {len(sessions)} in the calendar (absences are left out)")
        names = {c["id"]: c.get("firstName", "?") for c in children}
        for session in sessions:
            child = names.get(session.child_id, "?")
            print(f"   {session.start:%a %Y-%m-%d %H:%M}-{session.end:%H:%M} {child}: {session.summary}")
            if details := ", ".join(line for line in (*session.description.splitlines(), session.location) if line):
                print(f"     {details}")
        # Anything but a usual day, to compare with what the portal's Agenda shows.
        for part in parts:
            usual = (part.get("plannedAttendanceSubCode") or {}).get("code") == "planned"
            if not usual or part.get("bookingType") or part.get("toConfirmProduct"):
                fields = {k: v for k, v in part.items() if v is not None and v is not False and v != []}
                print(f"   Not a usual day: {json.dumps(fields)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
