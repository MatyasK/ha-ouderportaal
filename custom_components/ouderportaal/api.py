"""Async client for the Konnect ouderportaal parent API.

Kept free of Home Assistant imports so it can be tested and used standalone.

Auth flow (taken from the portal's own web app):
- PUT /auth-api/login with username/password returns a short-lived JWT (15 min)
  and sets an HttpOnly ``__Host-refresh_token`` cookie (1 hour).
- PUT /auth-api/token with the JWT and the cookie returns a new JWT.
  A 400/401 there means the session is gone and a fresh login is needed.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

import aiohttp

REFRESH_COOKIE = "__Host-refresh_token"
USER_AGENT = "HomeAssistant-Ouderportaal"
# Refresh this long before the JWT actually expires.
EXPIRY_MARGIN_S = 60

_PORTAL_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class OuderportaalError(Exception):
    """Base error."""


class OuderportaalConnectionError(OuderportaalError):
    """The portal could not be reached or answered unexpectedly."""


class OuderportaalAuthError(OuderportaalError):
    """The credentials were rejected."""


def normalize_portal(value: str) -> str:
    """Turn 'name', 'name.ouderportaal.nl' or a full portal URL into 'name'."""
    value = value.strip().lower()
    if "://" in value:
        value = urlsplit(value).hostname or ""
    value = value.split("/")[0].removesuffix(".ouderportaal.nl")
    if not _PORTAL_RE.match(value):
        raise ValueError(f"Not a valid portal name: {value!r}")
    return value


class OuderportaalClient:
    """Talks to https://<portal>.ouderportaal.nl.

    The session must not carry its own cookie jar for this host (use a
    DummyCookieJar): the refresh cookie is managed here explicitly.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        portal: str,
        username: str,
        password: str,
        *,
        base_url: str | None = None,
    ) -> None:
        self._session = session
        self.portal = normalize_portal(portal)
        # Override only for tests against a fake portal.
        self.base_url = base_url or f"https://{self.portal}.ouderportaal.nl"
        self._username = username
        self._password = password
        self._auth_token: str | None = None
        self._expires_at: float = 0
        self._refresh_cookie: str | None = None
        self._auth_lock = asyncio.Lock()

    async def login(self) -> None:
        """Log in with username and password."""
        data = await self._auth_call(
            "login",
            {"username": self._username, "password": self._password, "childMinder": False},
            use_session=False,
        )
        if data is None:
            raise OuderportaalAuthError("Username or password rejected")
        if data.get("mustResetPassword"):
            raise OuderportaalAuthError("The portal asks for a password reset; log in via the website first")

    async def _refresh(self) -> bool:
        """Get a new JWT using the refresh cookie. False if the session expired."""
        if not self._auth_token or not self._refresh_cookie:
            return False
        return await self._auth_call("token", {}, use_session=True) is not None

    async def _auth_call(self, endpoint: str, body: dict[str, Any], *, use_session: bool) -> dict[str, Any] | None:
        """PUT an auth-api endpoint; store the token. None on 400/401/403."""
        headers = self._base_headers()
        if use_session:
            headers["Authorization"] = f"Bearer {self._auth_token}"
            headers["Cookie"] = f"{REFRESH_COOKIE}={self._refresh_cookie}"
        try:
            async with self._session.put(
                f"{self.base_url}/auth-api/{endpoint}",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status in (400, 401, 403):
                    return None
                if resp.status != 200:
                    raise OuderportaalConnectionError(f"auth-api/{endpoint} returned HTTP {resp.status}")
                data = await resp.json(content_type=None)
                if cookie := resp.cookies.get(REFRESH_COOKIE):
                    self._refresh_cookie = cookie.value
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OuderportaalConnectionError(f"auth-api/{endpoint} failed: {err}") from err

        token = data.get("authToken") if isinstance(data, dict) else None
        if not token:
            raise OuderportaalConnectionError(f"auth-api/{endpoint} returned no token")
        self._auth_token = token
        # 'expiration' is epoch milliseconds; assume 15 minutes if missing.
        expiration = data.get("expiration")
        self._expires_at = expiration / 1000 if expiration else time.time() + 15 * 60
        return data

    async def _ensure_token(self, *, force: bool = False) -> str:
        async with self._auth_lock:
            expired = time.time() >= self._expires_at - EXPIRY_MARGIN_S
            if (force or not self._auth_token or expired) and not await self._refresh():
                await self.login()
            assert self._auth_token
            return self._auth_token

    def _base_headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Referer": f"{self.base_url}/parent/",
        }

    async def _get(self, path: str) -> Any:
        """GET a restservices-parent path and unwrap the {result, payload} envelope."""
        url = f"{self.base_url}/restservices-parent{path}"
        for attempt in range(2):
            token = await self._ensure_token(force=attempt > 0)
            headers = self._base_headers() | {"Authorization": f"Bearer {token}"}
            try:
                async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 401 and attempt == 0:
                        continue
                    if resp.status != 200:
                        raise OuderportaalConnectionError(f"{path} returned HTTP {resp.status}")
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise OuderportaalConnectionError(f"{path} failed: {err}") from err
            if isinstance(data, dict) and "payload" in data:
                if data.get("result") is False:
                    raise OuderportaalConnectionError(f"{path} returned result=false: {data.get('messages')}")
                return data["payload"]
            return data
        raise OuderportaalAuthError(f"{path} still unauthorized after logging in again")

    async def get_children(self) -> list[dict[str, Any]]:
        payload = await self._get("/children/")
        return list((payload or {}).get("activeChildren") or [])

    async def get_timeline(self, offset: int = 0) -> list[dict[str, Any]]:
        """Timeline cards, newest day first, starting at card number 'offset' (not a page number).

        The portal's infinite scroll asks for offset 0, then offset += number of cards received.
        """
        return list(await self._get(f"/timeline/cards/v2/{offset}") or [])
