"""Support for the EPH Controls Ember themostats."""

from __future__ import annotations

from datetime import timedelta
from enum import IntEnum
import logging
from typing import Any

from pyephember2.pyephember2 import (
    EphEmber,
    ZoneMode,
    ZoneCommand,
    boiler_state,
    zone_current_temperature,
    zone_is_hotwater,
    zone_is_boost_active,
    zone_mode,
    zone_name,
    zone_target_temperature,
)
import requests
import voluptuous as vol


def _patched_set_zone_boost(self, zone, boost_temperature, num_hours, timestamp=0):
    """Patched version of _set_zone_boost that fixes missing index argument.
    
    The original pyephember2 library has a bug where ZoneCommand is called
    with only 2 arguments, but the namedtuple requires 3 (name, value, index).
    Passing None for index allows zone_command_to_ints to fall back to
    GetPointIndex() to determine the correct index.
    """
    import datetime
    
    # Fix: Pass None as third argument (index) - the library will use GetPointIndex fallback
    cmds = [ZoneCommand('BOOST_HOURS', num_hours, None)]
    if boost_temperature is not None:
        cmds.append(ZoneCommand('BOOST_TEMP', boost_temperature, None))
    if timestamp is not None:
        if timestamp == 0:
            timestamp = int(datetime.datetime.now().timestamp())
        cmds.append(ZoneCommand('BOOST_TIME', timestamp, None))
    return self.messenger.send_zone_commands(zone, cmds)


# Monkey-patch the broken method in pyephember2
EphEmber._set_zone_boost = _patched_set_zone_boost

from homeassistant.components.climate import (
    PLATFORM_SCHEMA as CLIMATE_PLATFORM_SCHEMA,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
    PRESET_BOOST,
    PRESET_NONE
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_PASSWORD,
    CONF_USERNAME,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import EphemberConfigEntry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Return cached results if last scan was less then this time ago
SCAN_INTERVAL = timedelta(seconds=120)

OPERATION_LIST = [HVACMode.HEAT_COOL, HVACMode.HEAT, HVACMode.OFF]

PLATFORM_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_USERNAME): cv.string, vol.Required(CONF_PASSWORD): cv.string}
)

EPH_TO_HA_STATE = {
    "AUTO": HVACMode.HEAT_COOL,
    "ON": HVACMode.HEAT,
    "OFF": HVACMode.OFF,
}


class EPHBoilerStates(IntEnum):
    """Boiler states for a zone given by the api."""

    FIXME = 0
    OFF = 1
    ON = 2


HA_STATE_TO_EPH = {value: key for key, value in EPH_TO_HA_STATE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember climate from a config entry."""
    ember = entry.runtime_data

    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError:
        _LOGGER.error("Failed to get zones from EPH Controls")
        return

    entities = [
        EphEmberThermostat(ember, zone)
        for home in homes
        for zone in home["zones"]
    ]
    async_add_entities(entities)


def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the ephember thermostat via YAML (legacy)."""
    username = config.get(CONF_USERNAME)
    password = config.get(CONF_PASSWORD)

    try:
        ember = EphEmber(username, password)
    except RuntimeError:
        _LOGGER.error("Cannot login to EphEmber")
        return

    try:
        homes = ember.get_zones()
    except RuntimeError:
        _LOGGER.error("Failed to get zones")
        return

    add_entities(
        EphEmberThermostat(ember, zone) for home in homes for zone in home["zones"]
    )


class EphEmberThermostat(ClimateEntity):
    """Representation of a EphEmber thermostat."""

    _attr_hvac_modes = OPERATION_LIST
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST]
    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name

    def __init__(self, ember, zone) -> None:
        """Initialize the thermostat."""
        self._ember = ember
        self._zone_name = zone_name(zone)
        self._zone = zone
        self._zone_id = zone["zoneid"]
        self._attr_unique_id = self._zone_id

        # hot water = true, is immersive device without target temperature control.
        self._hot_water = zone_is_hotwater(zone)

        self._attr_supported_features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.PRESET_MODE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        self._attr_target_temperature_step = 0.5

        # Device info for device registry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=self._zone_name,
            manufacturer="EPH Controls",
            model=self._get_device_model(zone.get("deviceType")),
        )

    @staticmethod
    def _get_device_model(device_type: int | None) -> str:
        """Get human-readable model name from device type code."""
        device_models = {
            2: "Thermostat",
            4: "Hot Water Controller",
            514: "Hot Water Controller",
            773: "Thermostatic Radiator Valve",
        }
        return device_models.get(device_type, f"Unknown ({device_type})")

    @property
    def preset_mode(self):
        """Return current active preset mode."""
        return PRESET_BOOST if zone_is_boost_active(self._zone) else PRESET_NONE

    def set_preset_mode(self, preset_mode):
        """Set new target preset mode."""
        if preset_mode == PRESET_BOOST:
            self._ember.activate_zone_boost(
                self._attr_unique_id, zone_target_temperature(self._zone)
            )
        else:
            self._ember.deactivate_zone_boost(self._attr_unique_id)
        
        # Clear library cache and refresh zone data to get updated state
        # Don't fail if refresh times out - the command was already sent
        try:
            self._ember.NextHomeUpdateDaytime = None
            self._ember.get_zones()
            self._zone = self._ember.get_zone(self._zone["zoneid"])
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as err:
            _LOGGER.debug("Timeout refreshing zone after set_preset_mode: %s", err)

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return zone_current_temperature(self._zone)

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return zone_target_temperature(self._zone)

    @property
    def hvac_action(self) -> HVACAction:
        """Return current HVAC action."""
        if boiler_state(self._zone) == EPHBoilerStates.ON:
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation ie. heat, cool, idle."""
        mode = zone_mode(self._zone)
        return self.map_mode_eph_hass(mode)

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the operation mode."""
        mode = self.map_mode_hass_eph(hvac_mode)
        if mode is not None:
            self._ember.set_zone_mode(self._zone["zoneid"], mode)
            # Refresh zone data to get updated state
            # Don't fail if refresh times out - the command was already sent
            try:
                self._ember.NextHomeUpdateDaytime = None
                self._ember.get_zones()
                self._zone = self._ember.get_zone(self._zone["zoneid"])
            except (requests.exceptions.Timeout, requests.exceptions.RequestException) as err:
                _LOGGER.debug("Timeout refreshing zone after set_hvac_mode: %s", err)
        else:
            _LOGGER.error("Invalid operation mode provided %s", hvac_mode)

    def set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        if self._hot_water:
            return

        if temperature == self.target_temperature:
            return

        if temperature > self.max_temp or temperature < self.min_temp:
            return

        self._ember.set_zone_target_temperature(self._zone["zoneid"], temperature)
        
        # Refresh zone data to get updated state
        # Don't fail if refresh times out - the command was already sent
        try:
            self._ember.NextHomeUpdateDaytime = None
            self._ember.get_zones()
            self._zone = self._ember.get_zone(self._zone["zoneid"])
        except (requests.exceptions.Timeout, requests.exceptions.RequestException) as err:
            _LOGGER.debug("Timeout refreshing zone after set_temperature: %s", err)

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        # Hot water temp doesn't support being changed
        if self._hot_water:
            return zone_target_temperature(self._zone)

        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        if self._hot_water:
            return zone_target_temperature(self._zone)

        return 35.0

    def update(self) -> None:
        """Get the latest data."""
        try:
            self._ember.get_zones()
            self._zone = self._ember.get_zone(self._zone["zoneid"])
        except requests.exceptions.Timeout as err:
            _LOGGER.debug("Timeout updating zone %s: %s", self._zone_name, err)
        except requests.exceptions.RequestException as err:
            _LOGGER.debug("Network error updating zone %s: %s", self._zone_name, err)
        except (TimeoutError, OSError) as err:
            _LOGGER.debug("Connection error updating zone %s: %s", self._zone_name, err)
        except RuntimeError as err:
            _LOGGER.warning("Error updating zone %s: %s", self._zone_name, err)

    @staticmethod
    def map_mode_hass_eph(operation_mode):
        """Map from Home Assistant mode to eph mode."""
        return getattr(ZoneMode, HA_STATE_TO_EPH.get(operation_mode), None)

    @staticmethod
    def map_mode_eph_hass(operation_mode):
        """Map from eph mode to Home Assistant mode."""
        if operation_mode is None:
            return HVACMode.HEAT_COOL
        return EPH_TO_HA_STATE.get(operation_mode.name, HVACMode.HEAT_COOL)
