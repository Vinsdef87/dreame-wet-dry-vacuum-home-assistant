"""Dreame wet & dry vacuum integration for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import voluptuous as vol
from homeassistant.helpers import config_validation as cv
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DreameAPI, DreameAPIError, DreameAuthError
from .const import (
    CLEAN_MODE_PERSONALIZED,
    CONF_DEVICE_ID,
    CONF_REGION,
    DOMAIN,
    PROP_CLEAN_MODE,
    PROP_CLEAN_POWER,
    PROP_CLEAN_WATER,
    PROP_CUSTOM_SWITCH,
    PROP_ELEC_WATER,
    SERVICE_APPLY_CUSTOM_MODE,
)
from .coordinator import DreameWetDryCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
]

type DreameWetDryConfigEntry = ConfigEntry[DreameWetDryCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

APPLY_CUSTOM_MODE_SCHEMA = vol.Schema(
    {
        vol.Required("suction"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Required("water"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Required("electrolyzed_water"): cv.boolean,
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration-level services."""

    async def _apply_custom_mode(call: ServiceCall) -> None:
        values = {
            PROP_CLEAN_POWER: call.data["suction"],
            PROP_CLEAN_WATER: call.data["water"],
            PROP_CLEAN_MODE: CLEAN_MODE_PERSONALIZED,
            PROP_CUSTOM_SWITCH: 1,
            PROP_ELEC_WATER: 0 if call.data["electrolyzed_water"] else 1,
        }
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("No Dreame wet & dry vacuum loaded")
        for entry in entries:
            if not await entry.runtime_data.async_set_props(values):
                raise HomeAssistantError(
                    f"{entry.title}: custom mode not acknowledged by the device"
                )

    hass.services.async_register(
        DOMAIN, SERVICE_APPLY_CUSTOM_MODE, _apply_custom_mode, schema=APPLY_CUSTOM_MODE_SCHEMA
    )

    async def _set_property(call: ServiceCall) -> None:
        key = (call.data["siid"], call.data["piid"])
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("No Dreame wet & dry vacuum loaded")
        for entry in entries:
            if not await entry.runtime_data.async_set_props({key: call.data["value"]}):
                raise HomeAssistantError(
                    f"{entry.title}: {key[0]}.{key[1]} not acknowledged by the device"
                )

    hass.services.async_register(
        DOMAIN,
        "set_property",
        _set_property,
        schema=vol.Schema(
            {
                vol.Required("siid"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                vol.Required("piid"): vol.All(vol.Coerce(int), vol.Range(min=1, max=200)),
                vol.Required("value"): vol.Coerce(int),
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DreameWetDryConfigEntry) -> bool:
    """Set up Dreame wet & dry vacuum from a config entry."""
    api = DreameAPI(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        region=entry.data.get(CONF_REGION, "eu"),
        session=async_get_clientsession(hass),
    )

    try:
        await api.login()
        devices = await api.get_devices()
    except DreameAuthError as err:
        raise ConfigEntryAuthFailed(f"Dreame credentials rejected: {err}") from err
    except DreameAPIError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Dreame cloud: {err}") from err

    device_id = entry.data[CONF_DEVICE_ID]
    device_info = next((d for d in devices if str(d.get("did")) == str(device_id)), {})

    api.set_bind_domain(device_info.get("bindDomain") or device_info.get("bind_domain"))
    coordinator = DreameWetDryCoordinator(hass, entry, api, device_id, device_info)

    # Raises ConfigEntryNotReady / ConfigEntryAuthFailed on failure
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    coordinator.start_polling()

    # Start the real-time MQTT feed. start() builds an SSL context (blocking
    # disk I/O), so keep it off the event loop.
    await hass.async_add_executor_job(coordinator.start_mqtt)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: DreameWetDryConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        coordinator.stop_polling()
        if coordinator.mqtt:
            await hass.async_add_executor_job(coordinator.mqtt.stop)
    return unloaded
