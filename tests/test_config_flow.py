"""Config flow and re-authentication."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ouderportaal.api import OuderportaalAuthError, OuderportaalConnectionError
from custom_components.ouderportaal.const import CONF_PORTAL, DOMAIN

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

USER_INPUT = {
    CONF_PORTAL: "https://Mijnopvang.ouderportaal.nl/parent/#/tabs/mychild",
    CONF_USERNAME: " parent@example.com ",
    CONF_PASSWORD: "secret",
}


@pytest.fixture(autouse=True)
def skip_setup():
    with patch("custom_components.ouderportaal.async_setup_entry", return_value=True):
        yield


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_creates_entry_with_normalized_portal(hass: HomeAssistant, mock_client) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "mijnopvang.ouderportaal.nl"
    assert result["data"] == {CONF_PORTAL: "mijnopvang", CONF_USERNAME: "parent@example.com", CONF_PASSWORD: "secret"}
    assert result["result"].unique_id == "mijnopvang:parent@example.com"


@pytest.mark.parametrize(
    ("side_effect", "error"),
    [
        (OuderportaalAuthError("nope"), "invalid_auth"),
        (OuderportaalConnectionError("down"), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_login_errors_then_recovers(hass: HomeAssistant, mock_client, side_effect, error) -> None:
    mock_client.login.side_effect = side_effect
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_invalid_portal(hass: HomeAssistant, mock_client) -> None:
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT | {CONF_PORTAL: "not a portal"}
    )

    assert result["errors"] == {CONF_PORTAL: "invalid_portal"}
    mock_client.login.assert_not_called()


async def test_account_without_children(hass: HomeAssistant, mock_client) -> None:
    mock_client.get_children.return_value = []
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["errors"] == {"base": "no_children"}


async def test_same_account_twice_aborts(hass: HomeAssistant, mock_client) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id="mijnopvang:parent@example.com").add_to_hass(hass)
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass: HomeAssistant, mock_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mijnopvang:parent@example.com",
        data={CONF_PORTAL: "mijnopvang", CONF_USERNAME: "parent@example.com", CONF_PASSWORD: "old"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_client.login.side_effect = OuderportaalAuthError("nope")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "wrong"})
    assert result["errors"] == {"base": "invalid_auth"}

    mock_client.login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new"
