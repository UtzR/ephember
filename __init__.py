"""The EPH Controls Ember integration."""

from __future__ import annotations

import logging

from pyephember2.pyephember2 import EphEmber

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE]

type EphemberConfigEntry = ConfigEntry[EphEmber]


async def async_setup_entry(hass: HomeAssistant, entry: EphemberConfigEntry) -> bool:
    """Set up EPH Controls Ember from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    try:
        ember = await hass.async_add_executor_job(EphEmber, username, password)
    except RuntimeError as err:
        raise ConfigEntryNotReady(f"Unable to connect to EPH Controls: {err}") from err

    # Store the client in runtime_data
    entry.runtime_data = ember

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EphemberConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
