"""Keeps a copy of every photo in Home Assistant's media folder.

Files land in <media>/ouderportaal/<YYYY-MM-DD>/<photo id>.<ext>, so they also show up under
Media > My media and in backups. Each file is downloaded once; the portal's signed URLs expire
within hours, so a download that fails is retried on a later poll with fresh URLs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from pathlib import Path

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ARCHIVE_DIR, MAX_PARALLEL_DOWNLOADS
from .timeline import Photo

_LOGGER = logging.getLogger(__name__)

MEDIA_SOURCE_DIR = "local"


class PhotoArchive:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        media_dir = hass.config.media_dirs.get(MEDIA_SOURCE_DIR) or hass.config.path("media")
        self.root = Path(media_dir) / ARCHIVE_DIR
        self._write_failing = False

    @property
    def write_failing(self) -> bool:
        """The last attempt to save a photo could not write to the media folder."""
        return self._write_failing

    def path(self, photo: Photo) -> Path:
        return self.root / photo.filename

    @staticmethod
    def media_url(photo: Photo) -> str:
        """Authenticated URL served by Home Assistant's media source (needs a signed path or login)."""
        return f"/media/{MEDIA_SOURCE_DIR}/{ARCHIVE_DIR}/{photo.filename}"

    @staticmethod
    def media_content_id(photo: Photo) -> str:
        return f"media-source://media_source/{MEDIA_SOURCE_DIR}/{ARCHIVE_DIR}/{photo.filename}"

    async def async_saved(self, photos: Iterable[Photo]) -> set[str]:
        """Ids of the photos that already have a copy."""
        photos = list(photos)
        return await self._hass.async_add_executor_job(lambda: {p.id for p in photos if self.path(p).is_file()})

    async def async_download_missing(self, photos: Iterable[Photo]) -> int:
        """Download photos without a copy that still have a portal URL. Returns how many were saved.

        Saving is best effort: when the media folder cannot be written to, the rest of the integration keeps working.
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
