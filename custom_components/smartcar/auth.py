from abc import ABC, abstractmethod
import logging

from aiohttp import ClientResponse, ClientSession

from .const import API_VERSION

_LOGGER = logging.getLogger(__name__)


class AbstractAuth(ABC):
    """Abstract class to make authenticated requests."""

    def __init__(self, websession: ClientSession, host: str) -> None:
        """Initialize the auth."""
        self._websession = websession
        self._host = host

    @abstractmethod
    async def async_get_access_token(self) -> str:
        """Return a valid access token."""

    async def async_get_user_id(self) -> str | None:
        """Return the Smartcar user_id to scope requests to.

        Returns:
            The Smartcar user identifier, or None if the auth flow has not
            yet captured one (e.g. during the initial config flow before the
            redirect from Connect has completed).
        """
        return None

    async def request(
        self,
        method: str,
        path: str,
        version: str = API_VERSION,
        **kwargs,  # noqa: ANN003
    ) -> ClientResponse:
        """Make a request.

        Returns:
            The client response.
        """
        access_token = await self.async_get_access_token()
        user_id = await self.async_get_user_id()
        headers = dict(kwargs.pop("headers", {}))
        headers["authorization"] = f"Bearer {access_token}"
        if user_id and "sc-user-id" not in {k.lower() for k in headers}:
            headers["sc-user-id"] = user_id

        url = (
            path
            if path.startswith(("http://", "https://"))
            else f"{self._host}/v{version}/{path.lstrip('/')}"
        )

        _LOGGER.debug(
            "HTTP %s %s %r headers=%r",
            method,
            url,
            kwargs,
            {k: v for k, v in headers.items() if k.lower() != "authorization"},
        )

        return await self._websession.request(
            method,
            url,
            **kwargs,
            headers=headers,
        )
