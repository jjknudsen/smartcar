"""Application credentials platform for Smartcar.

Smartcar v3 replaces the per-user OAuth authorization-code grant with an
application-level client_credentials grant plus a ``sc-user-id`` header. The
Connect flow still runs (so the end user grants vehicle permissions), but the
redirect now carries only ``state`` and ``user_id`` — not an exchangeable
``code``. We:

1. Register a custom HA HTTP view at ``/api/smartcar/callback`` so the
   ``user_id`` query parameter survives the redirect (the stock OAuth2
   callback view in homeassistant.helpers.config_entry_oauth2_flow strips
   everything except ``state`` and ``code``/``error``).
2. Subclass ``LocalOAuth2Implementation`` so token resolution / refresh runs
   the client_credentials grant against ``iam.smartcar.com`` and carries the
   captured ``user_id`` through the token dict.

The Smartcar dashboard's authorized redirect URI must therefore point at our
custom view, not the stock ``/auth/external/callback``. Setup instructions in
README.md reflect this.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from aiohttp import web
from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.components.http import KEY_HASS, HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.config_entry_oauth2_flow import (
    LocalOAuth2Implementation,
    _decode_jwt,
)
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import (
    CALLBACK_PATH,
    CONF_USER_ID,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
)

_LOGGER = logging.getLogger(__name__)
_CALLBACK_VIEW_REGISTERED = f"{DOMAIN}_callback_view_registered"


def _ensure_callback_view_registered(hass: HomeAssistant) -> None:
    """Register the Smartcar OAuth callback view at most once.

    HA only calls ``async_setup`` after a config entry exists, which means
    the view would not be reachable during the very first OAuth flow.
    ``async_get_authorization_server`` runs as part of the flow's
    authorize-URL construction, so registering here guarantees the
    callback path is live before Smartcar can redirect to it.
    """
    if hass.data.get(_CALLBACK_VIEW_REGISTERED):
        return
    hass.http.register_view(SmartcarOAuthCallbackView())
    hass.data[_CALLBACK_VIEW_REGISTERED] = True


def _redirect_url(hass: HomeAssistant) -> str:
    """Compute the externally reachable Smartcar callback URL.

    Prefers the Home Assistant Cloud (Nabu Casa) URL when available — that
    URL stays stable across DNS/IP/cert changes, so the value the user
    pastes into the Smartcar dashboard's *Authorized redirect URIs* field
    keeps working even if their dynamic-DNS hostname or local cert rotates.
    Falls back to ``external_url`` (then a placeholder) when Cloud is not
    active.
    """
    try:
        base = get_url(
            hass,
            allow_internal=False,
            allow_ip=False,
            allow_cloud=True,
            prefer_cloud=True,
            require_ssl=True,
        )
    except NoURLAvailableError:
        return f"https://YOUR_DOMAIN:PORT{CALLBACK_PATH}"
    return f"{base}{CALLBACK_PATH}"


class SmartcarOAuth2Implementation(LocalOAuth2Implementation):
    """Smartcar v3 OAuth2 implementation using client_credentials.

    Overrides:
    - ``redirect_uri``: points at our custom callback view so ``user_id``
      survives the redirect from Connect.
    - ``async_resolve_external_data``: extracts ``user_id`` from the redirect
      and obtains an application token via client_credentials.
    - ``_async_refresh_token``: re-runs client_credentials (v3 has no refresh
      tokens) and preserves the captured ``user_id``.
    """

    @property
    def redirect_uri(self) -> str:
        """Return the externally reachable redirect URI for Connect."""
        return _redirect_url(self.hass)

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Resolve callback parameters to a token dict.

        Smartcar v3 sends ``user_id`` in the redirect (and may also include
        ``code`` for backward compatibility, which we ignore — the token is
        obtained via client_credentials, not auth-code exchange).
        """
        user_id = external_data.get(CONF_USER_ID) if external_data else None
        if not user_id:
            msg = "Missing user_id from Smartcar Connect callback"
            raise ValueError(msg)

        token = await self._token_request({"grant_type": "client_credentials"})
        return {**token, CONF_USER_ID: user_id}

    async def _async_refresh_token(self, token: dict) -> dict:
        """Fetch a fresh application token, preserving the captured user_id."""
        new_token = await self._token_request({"grant_type": "client_credentials"})
        return {**new_token, CONF_USER_ID: token.get(CONF_USER_ID)}


async def async_get_auth_implementation(
    hass: HomeAssistant,
    auth_domain: str,
    credential: ClientCredential,
) -> SmartcarOAuth2Implementation:
    """Return the Smartcar-specific OAuth2 implementation.

    Returns:
        A SmartcarOAuth2Implementation bound to the supplied credentials.
    """
    return SmartcarOAuth2Implementation(
        hass,
        auth_domain,
        credential.client_id,
        credential.client_secret,
        OAUTH2_AUTHORIZE,
        OAUTH2_TOKEN,
    )


async def async_get_authorization_server(  # noqa: RUF029
    hass: HomeAssistant,
) -> AuthorizationServer:
    """Return Smartcar's OAuth2 authorization server endpoints."""
    _ensure_callback_view_registered(hass)
    return AuthorizationServer(
        authorize_url=OAUTH2_AUTHORIZE,
        token_url=OAUTH2_TOKEN,
    )


async def async_get_description_placeholders(  # noqa: RUF029
    hass: HomeAssistant,
) -> dict[str, str]:
    """Return description placeholders for the credentials dialog."""
    return {
        "more_info_url": "https://github.com/wbyoung/smartcar?tab=readme-ov-file#configuration",
        "oauth_creds_url": "https://dashboard.smartcar.com/team/applications",
        "redirect_url": _redirect_url(hass),
    }


class SmartcarOAuthCallbackView(HomeAssistantView):
    """Custom callback view that preserves ``user_id`` from Smartcar.

    HA's stock OAuth2 callback view at ``/auth/external/callback`` drops
    everything except ``state`` and ``code``/``error``. Smartcar v3 sends
    ``user_id`` in the redirect, which we need to attach as the
    ``sc-user-id`` header on every subsequent API call.
    """

    url = CALLBACK_PATH
    name = "api:smartcar:callback"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Handle the redirect from Smartcar Connect.

        Returns:
            A response that closes the popup/window the user was redirected
            in, after resuming the matching config flow.
        """
        if "state" not in request.query:
            return web.Response(text="Missing state parameter")

        hass = request.app[KEY_HASS]
        state = _decode_jwt(hass, request.query["state"])

        if state is None:
            return web.Response(
                text=(
                    "Invalid state. Is the redirect URL configured "
                    "correctly in your Smartcar dashboard?"
                ),
                status=400,
            )

        user_input: dict[str, Any] = {"state": state}

        # mirror the stock callback view's behaviour on top of capturing user_id
        if "user_id" in request.query:
            user_input[CONF_USER_ID] = request.query["user_id"]
        if "code" in request.query:
            user_input["code"] = request.query["code"]
        if "error" in request.query:
            user_input["error"] = request.query["error"]

        if (
            CONF_USER_ID not in user_input
            and "code" not in user_input
            and "error" not in user_input
        ):
            return web.Response(
                text="Missing user_id, code, or error parameter from Smartcar"
            )

        await hass.config_entries.flow.async_configure(
            flow_id=cast("str", state["flow_id"]), user_input=user_input
        )
        _LOGGER.debug("Resumed Smartcar OAuth configuration flow")
        return web.Response(
            headers={"content-type": "text/html"},
            text="<script>window.close()</script>",
        )
