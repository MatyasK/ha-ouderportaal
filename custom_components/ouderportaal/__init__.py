"""The Ouderportaal (Konnect) integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .api import OuderportaalClient
from .const import CONF_PORTAL, DOMAIN
from .coordinator import OuderportaalConfigEntry, OuderportaalCoordinator

PLATFORMS = [Platform.CALENDAR, Platform.IMAGE, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def create_client(hass: HomeAssistant, portal: str, username: str, password: str) -> OuderportaalClient:
    # The client manages the refresh cookie itself, so the session must not keep cookies.
    session = async_create_clientsession(hass, cookie_jar=aiohttp.DummyCookieJar())
    return OuderportaalClient(session, portal, username, password)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> bool:
    client = create_client(hass, entry.data[CONF_PORTAL], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    coordinator = OuderportaalCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Cancelled automatically when the entry unloads.
    entry.async_create_background_task(hass, coordinator.async_backfill(), f"{DOMAIN} history import")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
