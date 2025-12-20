"""Support for the EPH Controls Ember themostats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import IntEnum
import logging
from typing import Any, Callable

from .pyephember2.pyephember2 import (
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
    # Fix: Pass None as third argument (index) - the library will use GetPointIndex fallback
    cmds = [ZoneCommand('BOOST_HOURS', num_hours, None)]
    if boost_temperature is not None:
        cmds.append(ZoneCommand('BOOST_TEMP', boost_temperature, None))
    if timestamp is not None:
        if timestamp == 0:
            timestamp = int(datetime.now(timezone.utc).timestamp())
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
from .const import CONF_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Default scan interval (will be overridden by config)
SCAN_INTERVAL = timedelta(seconds=300)

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
    data = entry.runtime_data
    ember = data.ember

    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError:
        _LOGGER.error("Failed to get zones from EPH Controls")
        return

    entities = [
        EphEmberThermostat(data, ember, zone, entry)
        for home in homes
        for zone in home["zones"]
    ]
    
    # Register entities in data structure for MQTT callbacks
    for entity in entities:
        data.zone_id_to_entity[entity._zone_id] = entity
    
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

    # Create minimal data object for legacy setup
    from . import EphemberData
    data = EphemberData(ember)

    add_entities(
        EphEmberThermostat(data, ember, zone, None) for home in homes for zone in home["zones"]
    )


class EphEmberThermostat(ClimateEntity):
    """Representation of a EphEmber thermostat."""

    _attr_hvac_modes = OPERATION_LIST
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST]
    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name

    def __init__(self, data, ember, zone, entry) -> None:
        """Initialize the thermostat."""
        self._data = data
        self._ember = ember
        self._entry = entry
        self._zone_name = zone_name(zone)
        self._zone = zone
        self._zone_id = zone["zoneid"]
        self._zone_mac = zone.get("mac")
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
            boost_temp = zone_target_temperature(self._zone)

            def _send(zone_id: str) -> bool:
                """Activate boost via MQTT for given zone id."""
                return self._ember.activate_zone_boost_mqtt(zone_id, boost_temp)

            self._call_mqtt_with_resync(_send)
        else:
            def _send(zone_id: str) -> bool:
                """Deactivate boost via MQTT for given zone id."""
                return self._ember.deactivate_zone_boost_mqtt(zone_id)

            self._call_mqtt_with_resync(_send)
        
        # Update timestamp
        if self._data:
            self._data.last_mqtt_sent = datetime.now(timezone.utc)

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

    def _call_mqtt_with_resync(self, send_func: Callable[[str], bool]) -> bool:
        """Call a MQTT action; on Unknown zone, resync HTTP and retry once."""
        try:
            # First attempt with current zone id
            return send_func(self._zone_id)
        except RuntimeError as err:
            # Only handle the specific "Unknown zone: ..." case
            if "Unknown zone" not in str(err):
                raise

            _LOGGER.debug(
                "Zone %s (MAC %s) unknown in Ember cache, attempting HTTP resync",
                self._zone_name,
                self._zone_mac,
            )

            # 1) Force HTTP refresh of homes/zones
            try:
                # Clear cache forcing fresh HTTP fetch
                self._ember.NextHomeUpdateDaytime = None
                self._ember.get_zones()
            except Exception as sync_err:  # pragma: no cover - defensive
                _LOGGER.warning(
                    "Failed to refresh zones from Ember after Unknown zone for %s: %s",
                    self._zone_name,
                    sync_err,
                )
                raise

            # 2) Re-find this zone by MAC in the refreshed data
            new_zone = None
            try:
                if self._zone_mac:
                    new_zone = self._ember.get_zone_by_mac(self._zone_mac)
            except Exception as find_err:  # pragma: no cover - defensive
                _LOGGER.warning(
                    "Failed to locate zone by MAC %s after resync for %s: %s",
                    self._zone_mac,
                    self._zone_name,
                    find_err,
                )

            if not new_zone:
                _LOGGER.error(
                    "Zone %s (MAC %s) still unknown after HTTP resync; cannot send MQTT command",
                    self._zone_name,
                    self._zone_mac,
                )
                raise

            # 3) Update local zone data & zone_id and retry once
            self._zone = new_zone
            self._zone_id = new_zone["zoneid"]

            _LOGGER.info(
                "Rebound zone %s to new zoneid %s after HTTP resync, retrying MQTT action",
                self._zone_name,
                self._zone_id,
            )

            return send_func(self._zone_id)

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the operation mode."""
        mode = self.map_mode_hass_eph(hvac_mode)
        if mode is not None:
            def _send(zone_id: str) -> bool:
                """Send MQTT command for given zone id."""
                if hvac_mode == HVACMode.OFF:
                    return self._ember.turn_zone_off_mqtt(zone_id)
                if hvac_mode == HVACMode.HEAT:
                    return self._ember.turn_zone_on_mqtt(zone_id)
                return self._ember.set_zone_mode_mqtt(zone_id, mode)

            self._call_mqtt_with_resync(_send)
            
            # Update timestamp
            if self._data:
                self._data.last_mqtt_sent = datetime.now(timezone.utc)
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
        
        def _send(zone_id: str) -> bool:
            """Send target temperature via MQTT for given zone id."""
            return self._ember.set_zone_target_temperature_mqtt(zone_id, temperature)

        self._call_mqtt_with_resync(_send)
        
        # Update timestamp
        if self._data:
            self._data.last_mqtt_sent = datetime.now(timezone.utc)

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
            self._zone = self._ember.get_zone(self._zone_id)
            # Update HTTP request timestamp
            if self._data:
                self._data.last_http_request = datetime.now(timezone.utc)
        except requests.exceptions.Timeout as err:
            _LOGGER.debug("Timeout updating zone %s: %s", self._zone_name, err)
        except requests.exceptions.RequestException as err:
            _LOGGER.debug("Network error updating zone %s: %s", self._zone_name, err)
        except (TimeoutError, OSError) as err:
            _LOGGER.debug("Connection error updating zone %s: %s", self._zone_name, err)
        except RuntimeError as err:
            # Check if it's a server error (e.g., 502 Bad Gateway)
            error_str = str(err)
            if "response code" in error_str:
                # Server errors (5xx) are temporary and should be logged at debug level
                # since we have MQTT as backup for real-time updates
                _LOGGER.debug("Server error updating zone %s: %s", self._zone_name, err)
            else:
                # Other RuntimeErrors might be more serious
                _LOGGER.warning("Error updating zone %s: %s", self._zone_name, err)

    @staticmethod
    def _time_units_to_hhmm(time_units: int) -> str:
        """
        Convert schedule time format to HH:MM.
        The API uses a format where the integer represents HHMM where the last digit
        is 10-minute units. For example: 90 = 09:00, 100 = 10:00, 173 = 17:30.
        This matches the scheduletime_to_time function in pyephember2.
        """
        if time_units is None or time_units < 0:
            return "00:00"
        # Convert to string to extract digits
        time_str = str(time_units)
        if len(time_str) == 0:
            return "00:00"
        # Last digit is 10-minute units, rest is hours
        hours = int(time_str[:-1]) if len(time_str) > 1 else 0
        minutes = 10 * int(time_str[-1])
        return f"{hours:02d}:{minutes:02d}"

    @staticmethod
    def _format_period(period: dict[str, Any]) -> str | None:
        """Format a single schedule period (p1, p2, or p3) to 'HH:MM-HH:MM'."""
        if not period:
            return None
        start_time = period.get("startTime")
        end_time = period.get("endTime")

        # Disabled / empty period
        if start_time is None or end_time is None or start_time == end_time:
            return None

        start_str = EphEmberThermostat._time_units_to_hhmm(start_time)
        end_str = EphEmberThermostat._time_units_to_hhmm(end_time)
        return f"{start_str}-{end_str}"

    @staticmethod
    def _format_day_schedule(day_data: dict[str, Any]) -> dict[str, str | None]:
        """Format one day's schedule (p1, p2, p3) into a dict."""
        return {
            "p1": EphEmberThermostat._format_period(day_data.get("p1", {})),
            "p2": EphEmberThermostat._format_period(day_data.get("p2", {})),
            "p3": EphEmberThermostat._format_period(day_data.get("p3", {})),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes, including the zone's schedule."""
        attrs: dict[str, Any] = {}

        device_days = self._zone.get("deviceDays", [])
        if device_days:
            # dayType: 0=Sunday ... 6=Saturday
            day_names = [
                "Sunday",
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
            ]
            schedule: dict[str, dict[str, str | None]] = {}

            for day_data in device_days:
                day_type = day_data.get("dayType")
                if isinstance(day_type, int) and 0 <= day_type <= 6:
                    day_name = day_names[day_type]
                    schedule[day_name] = self._format_day_schedule(day_data)

            if schedule:
                attrs["schedule"] = schedule

        return attrs

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
