"""Config flow for EPH Controls Ember integration."""

from __future__ import annotations

import logging
from typing import Any

from .pyephember2.pyephember2 import EphEmber
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import CONF_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=300): vol.All(int, vol.Range(min=60, max=3600)),
    }
)


class EphemberConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EPH Controls Ember."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Test the credentials
            try:
                ember = await self.hass.async_add_executor_job(
                    EphEmber, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                # Try to get zones to verify connection works
                await self.hass.async_add_executor_job(ember.get_zones)
            except RuntimeError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "cannot_connect"
            else:
                # Check if already configured
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()

                # Store scan_interval in options, credentials in data
                data = {CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]}
                options = {CONF_SCAN_INTERVAL: user_input.get(CONF_SCAN_INTERVAL, 300)}
                
                return self.async_create_entry(
                    title=user_input[CONF_USERNAME],
                    data=data,
                    options=options,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow for this handler."""
        return EphemberOptionsFlowHandler(config_entry)


class EphemberOptionsFlowHandler(OptionsFlow):
    """Handle options flow for EPH Controls Ember."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        # config_entry parameter is accepted but not stored
        # It's available as self.config_entry property from OptionsFlow base class
        pass

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(CONF_SCAN_INTERVAL, 300),
                    ): vol.All(int, vol.Range(min=60, max=3600)),
                }
            ),
        )

