"""Config flow: portal name, username and password."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from . import create_session
from .api import OuderportaalAuthError, OuderportaalClient, OuderportaalError, normalize_portal
from .const import CONF_PORTAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORTAL): str,
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        )
    }
)


class OuderportaalConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    # 2: a child's device and entity ids include the account.
    MINOR_VERSION = 2

    async def _validate(self, portal: str, username: str, password: str) -> dict[str, str]:
        """Log in and check there is at least one child. Returns form errors."""
        # Not an entry's setup, so nothing would close the session for us.
        session = create_session(self.hass, auto_cleanup=False)
        try:
            client = OuderportaalClient(session, portal, username, password)
            await client.login()
            children = await client.get_children()
        except OuderportaalAuthError:
            return {"base": "invalid_auth"}
        except OuderportaalError:
            return {"base": "cannot_connect"}
        except Exception:
            _LOGGER.exception("Unexpected error while logging in")
            return {"base": "unknown"}
        finally:
            session.detach()
        if not children:
            return {"base": "no_children"}
        return {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                portal = normalize_portal(user_input[CONF_PORTAL])
            except ValueError:
                errors[CONF_PORTAL] = "invalid_portal"
            else:
                username = user_input[CONF_USERNAME].strip()
                await self.async_set_unique_id(f"{portal}:{username.lower()}")
                self._abort_if_unique_id_configured()
                # One parent's account already shows every child, so a second account would only duplicate it.
                if any(e.data[CONF_PORTAL] == portal for e in self._async_current_entries(include_ignore=False)):
                    return self.async_abort(reason="portal_already_configured")
                errors = await self._validate(portal, username, user_input[CONF_PASSWORD])
                if not errors:
                    return self.async_create_entry(
                        title=f"{portal}.ouderportaal.nl",
                        data={CONF_PORTAL: portal, CONF_USERNAME: username, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._validate(entry.data[CONF_PORTAL], entry.data[CONF_USERNAME], user_input[CONF_PASSWORD])
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"username": entry.data[CONF_USERNAME], "portal": entry.data[CONF_PORTAL]},
        )
