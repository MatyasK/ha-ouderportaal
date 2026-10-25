"""Keeps a copy of every photo in a private folder in Home Assistant's config folder.

Files land in <config>/ouderportaal/<YYYY-MM-DD>/<photo id>.<ext>, so they are in backups. The portal shows a photo
only to the parent who is logged in, and it can show other children, so no media source exposes the folder: the
latest photo is served by the image entity, and any saved photo by PhotoView, to admins only. Each file is downloaded
once; the portal's signed URLs expire within hours, so a download that fails is retried on a later poll with fresh
URLs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView, require_admin
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.http import KEY_HASS

from .const import ARCHIVE_DIR, MAX_PARALLEL_DOWNLOADS
from .timeline import Photo

_LOGGER = logging.getLogger(__name__)

PHOTO_URL = f"/api/{ARCHIVE_DIR}/photos"


def archive_root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(ARCHIVE_DIR))


class PhotoArchive:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self.root = archive_root(hass)
        self._write_failing = False

    @property
    def write_failing(self) -> bool:
        """The last attempt to save a photo could not write to the archive folder."""
        return self._write_failing

    def path(self, photo: Photo) -> Path:
        return self.root / photo.filename

    @staticmethod
    def url(photo: Photo) -> str:
        """Served by PhotoView to admins: with their login, or signed with `auth/sign_path`."""
        return f"{PHOTO_URL}/{photo.filename}"

    async def async_saved(self, photos: Iterable[Photo]) -> set[str]:
        """Ids of the photos that already have a copy."""
        photos = list(photos)
        return await self._hass.async_add_executor_job(lambda: {p.id for p in photos if self.path(p).is_file()})

    async def async_download_missing(self, photos: Iterable[Photo]) -> int:
        """Download photos without a copy that still have a portal URL. Returns how many were saved.

        Saving is best effort: when the archive folder cannot be written to, the rest of the integration keeps working.
        """
        candidates = [p for p in photos if p.full_url]
        saved = await self.async_saved(candidates)
        missing = [p for p in candidates if p.id not in saved]
        if not missing:
            return 0
        # Folders first, so nothing is downloaded when they cannot be made.
        try:
            await self._hass.async_add_executor_job(self._make_folders, {self.path(p).parent for p in missing})
        except OSError as err:
            self._write_failed(err)
            return 0
        session = async_get_clientsession(self._hass)
        semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

        async def download(photo: Photo) -> bool:
            async with semaphore:
                return await self._download(session, photo)

        results = await asyncio.gather(*(download(p) for p in missing))
        return sum(results)

    async def _download(self, session: aiohttp.ClientSession, photo: Photo) -> bool:
        try:
            async with session.get(photo.full_url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    # Usually an expired signature; the next poll brings a fresh URL.
                    _LOGGER.debug("Photo %s not saved yet: HTTP %s", photo.id, resp.status)
                    return False
                content = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Photo %s not saved yet: %s", photo.id, err)
            return False
        try:
            await self._hass.async_add_executor_job(self._write, self.path(photo), content)
        except OSError as err:
            self._write_failed(err)
            return False
        if self._write_failing:
            _LOGGER.info("Saving photos works again")
            self._write_failing = False
        return True

    def _write_failed(self, err: OSError) -> None:
        # Every poll tries again; warn once.
        if not self._write_failing:
            _LOGGER.warning("Could not save photos in %s, will keep trying: %s", self.root, err)
        self._write_failing = True

    @staticmethod
    def _make_folders(folders: set[Path]) -> None:
        for folder in folders:
            folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        # Write next to the target and rename, so a crash never leaves half a photo behind.
        partial = path.with_name(path.name + ".part")
        partial.write_bytes(content)
        partial.replace(path)


class PhotoView(HomeAssistantView):
    """A saved photo, for admins only: other Home Assistant users may not see what the portal shows the parent."""

    url = PHOTO_URL + r"/{day:\d{4}-\d{2}-\d{2}}/{name:[A-Za-z0-9_-]{1,64}\.[a-z0-9]{1,5}}"
    name = f"api:{ARCHIVE_DIR}:photo"

    @require_admin
    async def get(self, request: web.Request, day: str, name: str) -> web.FileResponse:
        hass = request.app[KEY_HASS]
        # The route only matches a date and a plain file name, so the path stays inside the archive.
        path = archive_root(hass) / day / name
        if not await hass.async_add_executor_job(path.is_file):
            raise web.HTTPNotFound
        # Checked with the server on every use, so a browser shared with a non-admin does not show it from its cache.
        return web.FileResponse(path, headers={"Cache-Control": "private, no-cache"})
