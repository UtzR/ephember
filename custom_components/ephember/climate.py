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
    zone_supports_all_day,
    zone_target_temperature,
    zone_min_temperature,
    zone_max_temperature,
)
import requests
import voluptuous as vol

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
from .const import CONF_GATEWAY_ID, CONF_SCAN_INTERVAL, DOMAIN, EPHBoilerStates

_LOGGER = logging.getLogger(__name__)

# Default scan interval (will be overridden by config)
SCAN_INTERVAL = timedelta(seconds=300)

OPERATION_LIST = [HVACMode.HEAT, HVACMode.OFF]

PLATFORM_SCHEMA = CLIMATE_PLATFORM_SCHEMA.extend(
    {vol.Required(CONF_USERNAME): cv.string, vol.Required(CONF_PASSWORD): cv.string}
)

# Preset mode constants
PRESET_AUTO = "Auto"
PRESET_ALL_DAY = "All Day"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember climate from a config entry."""
    data = entry.runtime_data
    ember = data.ember
    selected_gateway_id = entry.data.get(CONF_GATEWAY_ID)

    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError as err:
        _LOGGER.error("Failed to get zones from EPH Controls: %s", err)
        return

    # Filter to selected home if gateway_id is specified
    if selected_gateway_id:
        homes = [home for home in homes if home.get("gatewayid") == selected_gateway_id]
        if not homes:
            _LOGGER.error("Selected home (gateway_id: %s) not found", selected_gateway_id)
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
    except RuntimeError as err:
        _LOGGER.error("Cannot login to EphEmber: %s", err)
        return

    try:
        homes = ember.get_zones()
    except RuntimeError as err:
        _LOGGER.error("Failed to get zones: %s", err)
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
    _attr_has_entity_name = True
    _attr_name = None  # Use device name as entity name
    _attr_translation_key = "ephember"  # Links to icons.json entity identifier

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
        self._device_type = zone.get("deviceType")

        # hot water = true, is immersive device without target temperature control.
        self._hot_water = zone_is_hotwater(zone)

        # Determine preset modes based on device type
        preset_modes = [PRESET_NONE, PRESET_BOOST, PRESET_AUTO]
        # Add ALL_DAY preset if device supports it
        if zone_supports_all_day(zone):
            preset_modes.append(PRESET_ALL_DAY)
        self._attr_preset_modes = preset_modes

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
            2: "Thermostat (type 2)",
            4: "Hot Water Controller (type 4)",
            258: "Thermostat (type 258)",
            514: "Thermostat (type 514)",
            516: "Hot Water Controller (type 516)",
            773: "Thermostatic Radiator Valve (type 773)",
        }
        return device_models.get(device_type, f"Unknown ({device_type})")


    @property
    def preset_mode(self):
        """Return current active preset mode."""
        mode = zone_mode(self._zone)
        
        # Check boost first (boost can be active in any mode)
        if zone_is_boost_active(self._zone):
            return PRESET_BOOST
        
        # Map zone modes to presets
        if mode == ZoneMode.AUTO:
            return PRESET_AUTO
        elif mode == ZoneMode.ALL_DAY:
            return PRESET_ALL_DAY
        elif mode == ZoneMode.ON:
            return PRESET_NONE  # ON without preset
        else:  # OFF or unknown
            return PRESET_NONE

    async def async_set_preset_mode(self, preset_mode):
        """Set new target preset mode."""
        if preset_mode == PRESET_BOOST:
            # Boost handling (existing logic)
            boost_temp = zone_target_temperature(self._zone)

            def _send(zone_id: str) -> bool:
                """Activate boost via MQTT for given zone id."""
                return self._ember._set_zone_boost(self._zone, boost_temp, num_hours=1, timestamp=0)

            await self._call_mqtt_with_resync(_send)
            
        elif preset_mode == PRESET_AUTO:
            # Set zone mode to AUTO
            def _send(zone_id: str) -> bool:
                """Set zone mode to AUTO via MQTT."""
                return self._ember._set_zone_mode(self._zone, ZoneMode.AUTO)
            
            await self._call_mqtt_with_resync(_send)
            
        elif preset_mode == PRESET_ALL_DAY:
            # Set zone mode to ALL_DAY (only for supported device types)
            if not zone_supports_all_day(self._zone):
                _LOGGER.warning(
                    "ALL_DAY mode not supported for deviceType %s", self._device_type
                )
                return
            
            def _send(zone_id: str) -> bool:
                """Set zone mode to ALL_DAY via MQTT."""
                return self._ember._set_zone_mode(self._zone, ZoneMode.ALL_DAY)
            
            await self._call_mqtt_with_resync(_send)
            
        elif preset_mode == PRESET_NONE:
            # Set zone mode to ON (manual ON, no preset)
            def _send(zone_id: str) -> bool:
                """Set zone mode to ON via MQTT."""
                return self._ember._set_zone_mode(self._zone, ZoneMode.ON)
            
            await self._call_mqtt_with_resync(_send)
            
            # Also deactivate boost if it was active
            if zone_is_boost_active(self._zone):
                def _send_boost_off(zone_id: str) -> bool:
                    """Deactivate boost via MQTT."""
                    return self._ember._set_zone_boost(self._zone, None, num_hours=0, timestamp=None)
                
                await self._call_mqtt_with_resync(_send_boost_off)
        else:
            _LOGGER.error("Invalid preset mode provided %s", preset_mode)
            return
        
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
        """Return current operation mode - OFF or ON (HEAT)."""
        mode = zone_mode(self._zone)
        
        # If boost is active, always show as HEAT
        if zone_is_boost_active(self._zone):
            return HVACMode.HEAT
        
        # If mode is OFF, return OFF
        if mode == ZoneMode.OFF:
            return HVACMode.OFF
        
        # All other modes (AUTO, ALL_DAY, ON) show as HEAT
        return HVACMode.HEAT

    def _is_setpoint_modification_enabled(self) -> bool:
        """Check if setpoint modification is enabled via the switch entity."""
        # Hot Water Controllers never have setpoint modification enabled
        if self._hot_water:
            return False
        
        if not self._data:
            return True  # Default to enabled for backward compatibility
        
        switch = self._data.zone_id_to_switch.get(self._zone_id)
        if switch is None:
            return True  # Default to enabled if switch doesn't exist
        
        return switch.is_on

    async def _call_mqtt_with_resync(self, send_func: Callable[[str], bool]) -> bool:
        """Call a MQTT action; on Unknown zone, resync HTTP and retry once."""
        try:
            # First attempt with current zone id (run in executor: may do MQTT connect → HTTP auth)
            return await self.hass.async_add_executor_job(send_func, self._zone_id)
        except RuntimeError as err:
            # Only handle the specific "Unknown zone: ..." case
            if "Unknown zone" not in str(err):
                raise

            _LOGGER.debug(
                "Zone %s (MAC %s) unknown in Ember cache, attempting HTTP resync",
                self._zone_name,
                self._zone_mac,
            )

            # 1) Force HTTP refresh of homes/zones (run in executor to avoid blocking event loop)
            try:
                # Clear cache forcing fresh HTTP fetch
                self._ember.NextHomeUpdateDaytime = None
                await self.hass.async_add_executor_job(self._ember.get_zones)
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

            return await self.hass.async_add_executor_job(send_func, self._zone_id)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the operation mode - OFF or ON."""
        if hvac_mode == HVACMode.OFF:
            # Set to OFF mode
            mode = ZoneMode.OFF
            
            # Cancel boost if it's active
            if zone_is_boost_active(self._zone):
                def _send_boost_off(zone_id: str) -> bool:
                    """Deactivate boost via MQTT."""
                    return self._ember._set_zone_boost(self._zone, None, num_hours=0, timestamp=None)
                
                await self._call_mqtt_with_resync(_send_boost_off)
        elif hvac_mode == HVACMode.HEAT:
            # Set to ON mode (preserves current zone mode if AUTO/ALL_DAY, otherwise just ON)
            current_mode = zone_mode(self._zone)
            if current_mode == ZoneMode.AUTO:
                mode = ZoneMode.AUTO
            elif current_mode == ZoneMode.ALL_DAY:
                mode = ZoneMode.ALL_DAY
            else:
                # ON without preset (or BOOST, which is separate)
                mode = ZoneMode.ON
        else:
            _LOGGER.error("Invalid operation mode provided %s", hvac_mode)
            return
        
        def _send(zone_id: str) -> bool:
            """Send MQTT command for given zone id."""
            return self._ember._set_zone_mode(self._zone, mode)

        await self._call_mqtt_with_resync(_send)
        
        # Update timestamp
        if self._data:
            self._data.last_mqtt_sent = datetime.now(timezone.utc)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        # Check if setpoint modification is enabled via switch
        if not self._is_setpoint_modification_enabled():
            return

        if temperature == self.target_temperature:
            return

        if temperature > self.max_temp or temperature < self.min_temp:
            return
        
        def _send(zone_id: str) -> bool:
            """Send target temperature via MQTT for given zone id."""
            # Use cached zone data instead of calling get_zone() which can trigger HTTP calls
            return self._ember._set_zone_target_temperature(self._zone, temperature)

        await self._call_mqtt_with_resync(_send)
        
        # Update timestamp
        if self._data:
            self._data.last_mqtt_sent = datetime.now(timezone.utc)

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        # If setpoint modification is disabled, return current target to disable UI
        if not self._is_setpoint_modification_enabled():
            return zone_target_temperature(self._zone) or 5.0

        return zone_min_temperature(self._zone)

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        # If setpoint modification is disabled, return current target to disable UI
        if not self._is_setpoint_modification_enabled():
            current_target = zone_target_temperature(self._zone)
            if current_target is not None:
                return current_target
            # Fallback based on device type
            return 60.0 if self._hot_water else 35.0

        return zone_max_temperature(self._zone)

    def update(self) -> None:
        """Get the latest data."""
        try:
            homes = self._ember.get_zones()
            self._zone = self._ember.get_zone(self._zone_id)
            # Update HTTP request timestamp and store zones data
            if self._data:
                self._data.last_http_request = datetime.now(timezone.utc)
                self._data.last_http_zones_data = homes
                
                # Update zone_heating cache for all zones from HTTP refresh (fallback)
                # This ensures heating sensors get updated even if MQTT misses messages
                for home in homes:
                    for zone in home.get("zones", []):
                        zid = zone.get("zoneid")
                        if zid:
                            try:
                                is_heating = (boiler_state(zone) == EPHBoilerStates.ON)
                                old_state = self._data.zone_heating.get(zid, False)
                                self._data.zone_heating[zid] = is_heating
                                
                                # Notify zone heating sensor if state changed
                                if old_state != is_heating:
                                    heating_sensor = self._data.zone_id_to_heating_sensor.get(zid)
                                    if heating_sensor is not None:
                                        # Schedule update on event loop (thread-safe)
                                        # This runs in executor thread, so schedule on event loop
                                        self.hass.loop.call_soon_threadsafe(
                                            heating_sensor.handle_zone_update, zone
                                        )
                            except Exception as err:
                                _LOGGER.debug(
                                    "Error updating zone_heating cache from HTTP for zone_id %s: %s",
                                    zid, err
                                )
                
                # Notify system heating sensor after all zones are updated
                if self._data.system_heating_sensor is not None:
                    self.hass.loop.call_soon_threadsafe(
                        self._data.system_heating_sensor.handle_system_update
                    )
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
    def _format_period_ts2(period: dict[str, Any]) -> str | None:
        """Format a single EMBER-TS2 schedule period to 'HH:MM'."""
        if not period:
            return None
        time_value = period.get("time")
        
        # Invalid period
        if time_value is None:
            return None
        
        return EphEmberThermostat._time_units_to_hhmm(time_value)

    @staticmethod
    def _get_schedule_type(device_type: int | None) -> str:
        """Get schedule type based on device type."""
        if device_type == 258:
            return "EMBER-TS"
        # deviceType 2, 4, 514, 516 (EMBER-PS/EMBER-PS2) or unknown
        return "EMBER-PS"

    @staticmethod
    def _format_day_schedule(day_data: dict[str, Any], device_type: int | None) -> dict[str, Any]:
        """Format one day's schedule into a dict.
        
        For EMBER-TS2 (deviceType 258): formats p1-p6 with time and temperature (t1-t6).
        For EMBER-PS/EMBER-PS2 (deviceType 2, 4, 514, 516): formats p1-p3 with startTime/endTime.
        """
        if device_type == 258:
            # EMBER-TS2 format: p1-p6 with time and temperature
            result = {}
            for i in range(1, 7):
                period_key = f"p{i}"
                temp_key = f"t{i}"
                period = day_data.get(period_key, {})
                
                # Format time
                result[period_key] = EphEmberThermostat._format_period_ts2(period)
                
                # Format temperature (divide by 10)
                temp = period.get("temperature")
                if temp is not None:
                    result[temp_key] = temp / 10.0
                else:
                    result[temp_key] = None
            
            return result
        else:
            # EMBER-PS/EMBER-PS2 format: p1-p3 with startTime/endTime
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
            # Extract deviceType to determine schedule format
            device_type = self._zone.get("deviceType")
            
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
            schedule: dict[str, Any] = {}
            
            # Add type field to schedule
            schedule["type"] = self._get_schedule_type(device_type)

            for day_data in device_days:
                day_type = day_data.get("dayType")
                if isinstance(day_type, int) and 0 <= day_type <= 6:
                    day_name = day_names[day_type]
                    schedule[day_name] = self._format_day_schedule(day_data, device_type)

            if schedule:
                attrs["schedule"] = schedule

        return attrs
