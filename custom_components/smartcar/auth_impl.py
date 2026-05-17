from typing import cast

from aiohttp import ClientSession
from homeassistant.helpers.config_entry_oauth2_flow import OAuth2Session

from .auth import AbstractAuth
from .const import CONF_USER_ID


class AsyncConfigEntryAuth(AbstractAuth):
    """Provide Smartcar authentication tied to an OAuth2 based config entry."""

    def __init__(
        self,
        websession: ClientSession,
        oauth_session: OAuth2Session,
        host: str,
    ) -> None:
        """Initialize Smartcar auth."""
        super().__init__(websession, host)
        self._oauth_session = oauth_session

    async def async_get_access_token(self) -> str:
        """Return a valid Smartcar application access token.

        v3 uses the OAuth 2.0 client_credentials grant; the Smartcar
        application token is independent of any individual user. The user
        binding happens via the ``sc-user-id`` header.
        """
        await self._oauth_session.async_ensure_token_valid()
        return cast("str", self._oauth_session.token["access_token"])

    async def async_get_user_id(self) -> str | None:
        """Return the captured Smartcar user_id, if any."""
        return self._oauth_session.token.get(CONF_USER_ID)


class AccessTokenAuthImpl(AbstractAuth):
    """Authentication implementation used during config flow, without refresh.

    Used by the config flow to call Smartcar before a full config entry
    exists. Does not support refreshing tokens — the caller has just
    obtained the token via client_credentials and it will be valid for at
    least an hour.
    """

    def __init__(
        self,
        websession: ClientSession,
        access_token: str,
        host: str,
        user_id: str | None = None,
    ) -> None:
        """Initialize the access-token-only auth implementation."""
        super().__init__(websession, host)
        self._access_token = access_token
        self._user_id = user_id

    async def async_get_access_token(self) -> str:
        """Return the access token."""
        return self._access_token

    async def async_get_user_id(self) -> str | None:
        """Return the captured Smartcar user_id, if any."""
        return self._user_id
