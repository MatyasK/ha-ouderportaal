"""Check the client against your real portal, outside Home Assistant.

    uv run --no-project --with aiohttp python scripts/try_portal.py mijnopvang you@example.com

Asks for your password, logs in, forces a token refresh, and prints the latest moments and messages.
Nothing is stored.
"""

from __future__ import annotations

import asyncio
import getpass
import importlib.util
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType
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

        today = date.today()
        items = await client.get_messages(today - timedelta(days=30), today + timedelta(days=1))
        messages = parse_messages(items, ZoneInfo("Europe/Amsterdam"))
        print(f"5. Messages, last 30 days: {len(items)} items, {len(messages)} understood")
        if items:
            first = items[0]
            print(f"   Fields: {', '.join(sorted(first))}")
            print(f"   'date' looks like: {type(first.get('date')).__name__} {first.get('date')!r}")
        for message in messages:
            print(f"   {message.sent:%Y-%m-%d %H:%M} {'*' if message.unread else ' '} {message.subject}")
        counts = await client.get_notifications()
        print(f"   Unread according to the portal: {counts.get('nrOfNewMessages') or 0}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
