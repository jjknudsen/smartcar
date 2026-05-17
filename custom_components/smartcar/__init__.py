import asyncio
from functools import partial
from http import HTTPStatus
import logging
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.components import cloud, webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    OAuth2Session,
    async_get_config_entry_implementation,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .application_credentials import SmartcarOAuthCallbackView
from .auth import AbstractAuth
from .auth_impl import AccessTokenAuthImpl, AsyncConfigEntryAuth  # noqa: F401
from .const import API_HOST, CONF_CLOUDHOOK, CONF_USER_ID, DOMAIN, PLATFORMS
from .coordinator import SmartcarVehicleCoordinator
from .errors import EmptyVehicleListError, InvalidAuthError, MissingVINError
from .services import async_setup_services
from .types import SmartcarData
from .webhooks import handle_webhook, webhook_url_from_id

_LOGGER = logging.getLogger(__name__)


async def async_setup(  # noqa: RUF029
    hass: HomeAssistant,
    config: ConfigType,  # noqa: ARG001
) -> bool:
    """Set up Smartcar services and the OAuth callback view.

    Returns:
        If the setup was successful.
    """
    async_setup_services(hass)
    hass.http.register_view(SmartcarOAuthCallbackView())

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Smartcar from a config entry.

    Returns:
        If the setup was successful.

    Raises:
        ConfigEntryError: For overlapping VIN in config entries.
    """
    implementation = await async_get_config_entry_implementation(hass, entry)
    websession = async_get_clientsession(hass)
    oauth_session = OAuth2Session(hass, entry, implementation)
    auth = AsyncConfigEntryAuth(websession, oauth_session, API_HOST)
    coordinators: dict[str, SmartcarVehicleCoordinator] = {}
    meta_coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_meta", config_entry=entry
    )
    meta_coordinator.async_set_updated_data({})
    entry.runtime_data = SmartcarData(
        auth=auth,
        coordinators=coordinators,
        meta_coordinator=meta_coordinator,
    )
    device_registry = dr.async_get(hass)
    other_vins = vehicle_vins_in_use(hass, entry)

    for vehicle_id, details in entry.data.get("vehicles", {}).items():
        vin = details["vin"]
        make = details.get("make")
        model = details.get("model")
        year = details.get("year")

        if vin in other_vins:
            msg = f"Cannot setup multiple config entries with VIN {vin}"
            raise ConfigEntryError(msg)

        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, vin)},
            manufacturer=make,
            model=f"{model} ({year})" if model and year else model,
            name=f"{make} {model}" if make and model else f"Smartcar {vin[-4:]}",
        )
        _LOGGER.info("Registered device for VIN: %s", vin)

        coordinator = SmartcarVehicleCoordinator(hass, auth, vehicle_id, vin, entry)
        coordinators[vin] = coordinator
        _LOGGER.debug("Coordinator created and initial data fetched for VIN: %s", vin)

    _LOGGER.debug("Forwarding setup to platforms: %s", PLATFORMS)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if CONF_WEBHOOK_ID in entry.data:
        _LOGGER.info(
            "Registering webhook at url: %s",
            (await webhook_url_from_id(hass, entry.data[CONF_WEBHOOK_ID]))[0],
        )
        webhook.async_register(
            hass,
            DOMAIN,
            entry.title,
            entry.data[CONF_WEBHOOK_ID],
            partial(handle_webhook, config_entry=entry),
        )
    else:
        _LOGGER.debug("Webhooks are not enabled")

    await asyncio.gather(
        *[async_do_first_refresh(coordinator) for coordinator in coordinators.values()]
    )

    _LOGGER.info(
        "Using token with scopes: %s", entry.data.get("token", {}).get("scopes")
    )

    entry.async_on_unload(
        entry.add_update_listener(
            partial(async_update_listener, initial_data=entry.data)
        )
    )

    return True


async def async_do_first_refresh(coordinator: SmartcarVehicleCoordinator) -> None:
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug(
        "Coordinator created and initial data fetched for VIN: %s", coordinator.vin
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    Returns:
        If the unload was successful.
    """
    _LOGGER.info("Unloading Smartcar entry %s", entry.entry_id)
    if CONF_WEBHOOK_ID in entry.data:
        webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID])
    return bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Cleanup when entry is removed."""
    if CONF_WEBHOOK_ID in entry.data and (
        cloud.async_active_subscription(hass) or entry.data.get(CONF_CLOUDHOOK, False)
    ):
        try:
            _LOGGER.debug(
                "Removing Smartcar cloudhook (%s)", entry.data[CONF_WEBHOOK_ID]
            )
            await cloud.async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
        except cloud.CloudNotAvailable:
            pass


async def async_update_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    initial_data: dict[str, Any],
) -> None:
    """Handle options update."""

    entry_data = {k: v for k, v in entry.data.items() if k != "token"}
    initial_data = {k: v for k, v in initial_data.items() if k != "token"}

    if entry_data != initial_data:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate config entries forward.

    Returns:
        True on a clean migration. Returns False to abort downgrades and to
        signal that v2→v3 entries need user-driven reauth (the legacy
        per-user OAuth token has no derivable Smartcar ``user_id`` and the
        v3 ``client_credentials`` flow needs one to scope API calls).
    """
    _LOGGER.debug(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )

    # prevent rollbacks
    if config_entry.version > 3:
        return False

    if config_entry.version == 1:
        # version 1 was the v2 API per-vehicle token; version 2 reshaped the
        # entry data but still used the v2 API. neither persists a user_id
        # for v3, so v3 needs an explicit reauth to capture it. mark the
        # entry as v3 and start reauth.
        hass.config_entries.async_update_entry(
            config_entry,
            version=3,
            minor_version=0,
        )
        config_entry.async_start_reauth(hass)
        _LOGGER.info(
            "Smartcar entry %s migrated v1→v3; reauth required to capture user_id",
            config_entry.entry_id,
        )
        return True

    if config_entry.version == 2:
        hass.config_entries.async_update_entry(
            config_entry,
            version=3,
            minor_version=0,
        )
        if CONF_USER_ID not in config_entry.data.get(CONF_TOKEN, {}):
            config_entry.async_start_reauth(hass)
            _LOGGER.info(
                "Smartcar entry %s migrated v2→v3; reauth required to capture user_id",
                config_entry.entry_id,
            )
        return True

    _LOGGER.debug(
        "Migration to configuration version %s.%s successful",
        config_entry.version,
        config_entry.minor_version,
    )

    return True


def vehicle_vins_in_use(
    hass: HomeAssistant, config_entry: ConfigEntry = None
) -> set[str]:
    return {
        vehicle["vin"]
        for other_entry in hass.config_entries.async_entries(DOMAIN)
        for vehicle in other_entry.data.get("vehicles", {}).values()
        if not config_entry or other_entry.unique_id != config_entry.unique_id
    }


async def populate_entry_data(
    data: dict,
    auth: AbstractAuth,
    user_id: str,
) -> None:
    """Populate config entry data during initial creation.

    Fetches the user's connections from v3 ``/connections`` and stores
    per-vehicle metadata (make/model/year/VIN) plus the union of permissions
    granted across those connections. The granted-permissions list takes the
    place of v2's per-token ``scope`` claim.

    Raises:
        EmptyVehicleListError: If no vehicles are connected for this user.
        InvalidAuthError: If the application token is rejected.
        MissingVINError: If the API does not return a VIN for some vehicle.
        ClientResponseError: For other transport-level errors.
    """
    data["vehicles"] = {}

    try:
        connections_resp = await auth.request(
            "get",
            "connections",
            params={"filter[userId]": user_id, "page[size]": 100},
        )
        connections_resp.raise_for_status()
        connections_data = await connections_resp.json()
    except ClientResponseError as err:
        if err.status == HTTPStatus.UNAUTHORIZED:
            msg = f"Auth error listing connections: {err.status}"
            raise InvalidAuthError(msg) from err
        raise

    connections = connections_data.get("data", [])
    _LOGGER.info("Found %s Smartcar connection(s) for user", len(connections))

    if not connections:
        raise EmptyVehicleListError

    permissions: set[str] = set()
    for connection in connections:
        attributes = connection.get("attributes", {}) or {}
        vehicle_attrs = attributes.get("vehicle", {}) or {}
        relationships = connection.get("relationships", {}) or {}
        vehicle_id = (
            relationships.get("vehicle", {}).get("data", {}).get("id")
        )
        if not vehicle_id:
            _LOGGER.warning(
                "Connection %s has no vehicle relationship; skipping",
                connection.get("id"),
            )
            continue

        permissions.update(attributes.get("permissions", []) or [])
        data["vehicles"][vehicle_id] = {
            "make": vehicle_attrs.get("make"),
            "model": vehicle_attrs.get("model"),
            "year": vehicle_attrs.get("year"),
        }

    data.setdefault("token", {})["scopes"] = sorted(permissions)

    if not data["vehicles"]:
        raise EmptyVehicleListError

    await asyncio.gather(
        *[
            _store_vehicle_vin(data, auth, vehicle_id)
            for vehicle_id in list(data["vehicles"].keys())
        ]
    )


async def _store_vehicle_vin(
    data: dict,
    auth: AbstractAuth,
    vehicle_id: str,
) -> None:
    """Fetch the VIN for a vehicle via the v3 signals endpoint.

    Raises:
        MissingVINError: If the response does not include a VIN.
        InvalidAuthError: If the request cannot be authorized.
        ClientResponseError: For other transport-level errors.
    """
    try:
        _LOGGER.debug("Fetching VIN for vehicle ID: %s", vehicle_id)
        resp = await auth.request(
            "get",
            f"vehicles/{vehicle_id}/signals/vehicleidentification-vin",
        )
        resp.raise_for_status()
        body = await resp.json()
        vin = (
            body.get("data", {})
            .get("attributes", {})
            .get("body", {})
            .get("value")
        )

        if not vin:
            msg = f"No VIN for vehicle {vehicle_id}"
            raise MissingVINError(msg)

        data["vehicles"][vehicle_id]["vin"] = vin
    except ClientResponseError as err:
        if err.status == HTTPStatus.UNAUTHORIZED:
            msg = f"Auth error [{err.status}] fetching VIN"
            raise InvalidAuthError(msg) from err
        raise
