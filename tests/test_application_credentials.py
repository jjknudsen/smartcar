"""Test application_credentials."""

from homeassistant.core import HomeAssistant
import pytest

from custom_components.smartcar.application_credentials import (
    async_get_description_placeholders,
)


@pytest.mark.parametrize(
    ("external_url", "expected_redirect_uri"),
    [
        ("https://example.com", "https://example.com/api/smartcar/callback"),
        (None, "https://YOUR_DOMAIN:PORT/api/smartcar/callback"),
    ],
)
async def test_description_placeholders(
    hass: HomeAssistant,
    external_url: str | None,
    expected_redirect_uri: str,
) -> None:
    """Test description placeholders.

    Smartcar v3 sends ``user_id`` in the Connect redirect, which the stock
    HA OAuth callback view drops. We register our own callback view at
    ``/api/smartcar/callback`` and surface that URL in the credentials
    setup dialog so users can configure it in the Smartcar dashboard.
    """
    hass.config.external_url = external_url
    placeholders = await async_get_description_placeholders(hass)
    assert placeholders == {
        "more_info_url": "https://github.com/wbyoung/smartcar?tab=readme-ov-file#configuration",
        "oauth_creds_url": "https://dashboard.smartcar.com/team/applications",
        "redirect_url": expected_redirect_uri,
    }
