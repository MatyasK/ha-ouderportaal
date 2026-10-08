"""The API client against a fake portal that mimics the real auth flow."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.ouderportaal.api import (
    REFRESH_COOKIE,
    OuderportaalAuthError,
    OuderportaalClient,
    OuderportaalConnectionError,
)

from .conftest import load_fixture

# The fake portal is a real local HTTP server; the HA test harness blocks sockets by default.
pytestmark = pytest.mark.usefixtures("socket_enabled")


@dataclass
class FakePortal:
    password: str = "secret"
    token_lifetime_s: float = 900
    refresh_allowed: bool = True
    timeline_status: int = 200
    calls: list[str] = field(default_factory=list)
    valid_tokens: set[str] = field(default_factory=set)
    valid_refresh: set[str] = field(default_factory=set)
    _counter: int = 0

    def _issue(self) -> web.Response:
        self._counter += 1
        token, refresh = f"jwt{self._counter}", f"refresh{self._counter}"
        self.valid_tokens = {token}
        self.valid_refresh = {refresh}
        resp = web.json_response(
            {"authToken": token, "expiration": (time.time() + self.token_lifetime_s) * 1000, "refreshToken": None}
        )
        resp.set_cookie(REFRESH_COOKIE, refresh, secure=True, httponly=True, path="/")
        return resp

    def _authorized(self, request: web.Request) -> bool:
        return request.headers.get("Authorization", "").removeprefix("Bearer ") in self.valid_tokens

    async def login(self, request: web.Request) -> web.Response:
        self.calls.append("login")
        body = await request.json()
        if body.get("password") != self.password or body.get("childMinder") is not False:
            return web.json_response({"message": "bad credentials"}, status=401)
        return self._issue()

    async def token(self, request: web.Request) -> web.Response:
        self.calls.append("refresh")
        if not self.refresh_allowed or request.cookies.get(REFRESH_COOKIE) not in self.valid_refresh:
            return web.json_response({}, status=401)
        return self._issue()

    async def children(self, request: web.Request) -> web.Response:
        self.calls.append("children")
        if not self._authorized(request):
            return web.json_response({}, status=401)
        return web.json_response(load_fixture("children.json"))

    async def timeline(self, request: web.Request) -> web.Response:
        self.calls.append("timeline")
        if not self._authorized(request):
            return web.json_response({}, status=401)
        if self.timeline_status != 200:
            return web.json_response({}, status=self.timeline_status)
        return web.json_response(load_fixture("timeline.json"))


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
async def client(portal: FakePortal) -> AsyncIterator[OuderportaalClient]:
    app = web.Application()
    app.router.add_put("/auth-api/login", portal.login)
    app.router.add_put("/auth-api/token", portal.token)
    app.router.add_get("/restservices-parent/children/", portal.children)
    app.router.add_get("/restservices-parent/timeline/cards/v2/{page}", portal.timeline)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as session:
        yield OuderportaalClient(
            session, "mijnopvang", "parent@example.com", "secret", base_url=str(server.make_url("")).rstrip("/")
        )
    await server.close()


async def test_logs_in_and_unwraps_payload(client: OuderportaalClient, portal: FakePortal) -> None:
    children = await client.get_children()

    assert [c["firstName"] for c in children] == ["Sam"]
    assert portal.calls == ["login", "children"]


async def test_reuses_token_while_valid(client: OuderportaalClient, portal: FakePortal) -> None:
    await client.get_children()
    await client.get_timeline()

    assert portal.calls == ["login", "children", "timeline"]


async def test_refreshes_with_cookie_when_token_expires(client: OuderportaalClient, portal: FakePortal) -> None:
    portal.token_lifetime_s = 0
    await client.get_children()
    portal.token_lifetime_s = 900

    cards = await client.get_timeline()

    assert len(cards) == 2
    assert portal.calls == ["login", "children", "refresh", "timeline"]


async def test_logs_in_again_when_refresh_is_rejected(client: OuderportaalClient, portal: FakePortal) -> None:
    portal.token_lifetime_s = 0
    await client.get_children()
    portal.token_lifetime_s = 900
    portal.refresh_allowed = False

    await client.get_timeline()

    assert portal.calls == ["login", "children", "refresh", "login", "timeline"]


async def test_retries_once_when_server_revokes_token(client: OuderportaalClient, portal: FakePortal) -> None:
    await client.get_children()
    portal.valid_tokens.clear()

    await client.get_timeline()

    assert portal.calls == ["login", "children", "timeline", "refresh", "timeline"]


async def test_wrong_password_raises_auth_error(client: OuderportaalClient, portal: FakePortal) -> None:
    portal.password = "changed"

    with pytest.raises(OuderportaalAuthError):
        await client.get_children()
    assert portal.calls == ["login"]


async def test_server_error_raises_connection_error(client: OuderportaalClient, portal: FakePortal) -> None:
    portal.timeline_status = 500

    with pytest.raises(OuderportaalConnectionError):
        await client.get_timeline()


async def test_unreachable_portal_raises_connection_error() -> None:
    async with aiohttp.ClientSession() as session:
        client = OuderportaalClient(session, "x", "u", "p", base_url="http://127.0.0.1:9")
        with pytest.raises(OuderportaalConnectionError):
            await client.get_children()
