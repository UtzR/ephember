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

from .const import CONF_GAS_CONSUMPTION_RATE, CONF_GATEWAY_ID, CONF_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=300): vol.All(int, vol.Range(min=60, max=3600)),
        vol.Optional(CONF_GAS_CONSUMPTION_RATE, default=1.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=10.0)
        ),
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
                homes = await self.hass.async_add_executor_job(ember.get_zones)
            except RuntimeError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "cannot_connect"
            else:
                # Check if already configured
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_configured()

                # Store credentials and options temporarily for next step
                self._username = user_input[CONF_USERNAME]
                self._password = user_input[CONF_PASSWORD]
                self._scan_interval = user_input.get(CONF_SCAN_INTERVAL, 300)
                self._gas_consumption_rate = user_input.get(CONF_GAS_CONSUMPTION_RATE, 1.5)
                self._homes = homes
                
                # If multiple homes, show selection step
                if len(homes) > 1:
                    return await self.async_step_select_home()
                
                # Single home - use it directly
                gateway_id = homes[0].get("gatewayid")
                if not gateway_id:
                    errors["base"] = "no_gateway_id"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=STEP_USER_DATA_SCHEMA,
                        errors=errors,
                    )
                
                # Store scan_interval and gas_consumption_rate in options, credentials and gateway_id in data
                data = {
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_GATEWAY_ID: gateway_id,
                }
                options = {
                    CONF_SCAN_INTERVAL: self._scan_interval,
                    CONF_GAS_CONSUMPTION_RATE: self._gas_consumption_rate,
                }
                
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

    async def async_step_select_home(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle home selection step when multiple homes exist."""
        if user_input is not None:
            gateway_id = user_input[CONF_GATEWAY_ID]
            
            # Store scan_interval and gas_consumption_rate in options, credentials and gateway_id in data
            data = {
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_GATEWAY_ID: gateway_id,
            }
            options = {
                CONF_SCAN_INTERVAL: self._scan_interval,
                CONF_GAS_CONSUMPTION_RATE: self._gas_consumption_rate,
            }
            
            return self.async_create_entry(
                title=self._username,
                data=data,
                options=options,
            )
        
        # Build selection schema
        home_options = {}
        for home in self._homes:
            gateway_id = home.get("gatewayid")
            if not gateway_id:
                continue
            name = home.get("name", f"Home {gateway_id}")
            device_type = home.get("deviceType")
            system_type = home.get("sysTemType", "")
            # Create display name: "Home Name (systemType, type X)"
            display_name = f"{name}"
            if system_type:
                display_name += f" ({system_type}"
                if device_type is not None:
                    display_name += f", type {device_type}"
                display_name += ")"
            home_options[gateway_id] = display_name
        
        return self.async_show_form(
            step_id="select_home",
            data_schema=vol.Schema({
                vol.Required(CONF_GATEWAY_ID): vol.In(home_options),
            }),
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
                    vol.Optional(
                        CONF_GAS_CONSUMPTION_RATE,
                        default=self.config_entry.options.get(CONF_GAS_CONSUMPTION_RATE, 1.5),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10.0)),
                }
            ),
        )

