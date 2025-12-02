"""Config flow for EPH Controls Ember integration."""

from __future__ import annotations

import logging
from typing import Any

from pyephember2.pyephember2 import EphEmber
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
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

                return self.async_create_entry(
                    title=f"EPH user: {user_input[CONF_USERNAME]}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

