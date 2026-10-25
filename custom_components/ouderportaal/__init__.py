"""The Ouderportaal (Konnect) integration."""

from __future__ import annotations

import aiohttp
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from . import websocket
from .api import OuderportaalClient
from .archive import PhotoView
from .const import CONF_PORTAL, DOMAIN
from .coordinator import OuderportaalConfigEntry, OuderportaalCoordinator, ScheduleCoordinator

PLATFORMS = [Platform.CALENDAR, Platform.IMAGE, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def create_session(hass: HomeAssistant, *, auto_cleanup: bool = True) -> aiohttp.ClientSession:
    """A session for one client. Created during an entry's setup, it is closed when the entry unloads.

    Without auto_cleanup, the caller detaches it when done.
    """
    # The client manages the refresh cookie itself, so the session must not keep cookies.
    return async_create_clientsession(hass, auto_cleanup=auto_cleanup, cookie_jar=aiohttp.DummyCookieJar())


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    websocket.async_register(hass)
    hass.http.register_view(PhotoView())
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> bool:
    if entry.version > 1:
        # Set up by a newer version of the integration.
        return False
    if entry.minor_version < 2:
        await _scope_ids_to_the_account(hass, entry)
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def _scope_ids_to_the_account(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> None:
    """Minor version 2: a child's device and entities are per account, back when both parents could add theirs.

    They keep their entity ids, names and areas.
    """
    prefix = f"{entry.entry_id}_"

    def scoped(entity: er.RegistryEntry) -> dict[str, str] | None:
        return None if entity.unique_id.startswith(prefix) else {"new_unique_id": prefix + entity.unique_id}

    await er.async_migrate_entries(hass, entry.entry_id, scoped)
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        old = {(domain, id_) for domain, id_ in device.identifiers if domain == DOMAIN and not id_.startswith(prefix)}
        if old:
            new = {(DOMAIN, prefix + id_) for _, id_ in old}
            devices.async_update_device(device.id, new_identifiers=(device.identifiers - old) | new)


async def async_setup_entry(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> bool:
    client = OuderportaalClient(
        create_session(hass), entry.data[CONF_PORTAL], entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD]
    )
    coordinator = OuderportaalCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    if coordinator.has_schedule:
        coordinator.schedule = ScheduleCoordinator(hass, entry, client)
        # Not the first-refresh variant: an Agenda that cannot be read must not hold up the rest. Its calendar is then
        # unavailable until a later refresh works.
        await coordinator.schedule.async_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Cancelled automatically when the entry unloads.
    entry.async_create_background_task(hass, coordinator.async_backfill(), f"{DOMAIN} history import")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OuderportaalConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
