"""Support for EPH Controls Ember sensors (diagnostics + heating state)."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import EphemberConfigEntry
from .const import CONF_GAS_CONSUMPTION_RATE, CONF_GATEWAY_ID, DOMAIN, EPHBoilerStates
from .pyephember2.pyephember2 import boiler_state, zone_name

_LOGGER = logging.getLogger(__name__)


def _build_device_model_string(data: Any) -> str | None:
    """
    Build the device model string from system_type and device_type.
    Matches the format used in __init__.py for the main device.
    """
    model_parts = []
    if getattr(data, "system_type", None):
        model_parts.append(data.system_type)
    if getattr(data, "device_type", None) is not None:
        model_parts.append(f"(type {data.device_type})")
    return " ".join(model_parts) if model_parts else None


def _zone_is_heating(zone: dict[str, Any]) -> bool:
    """Return True if zone boiler_state reports ON."""
    try:
        return boiler_state(zone) == EPHBoilerStates.ON
    except Exception:
        return False


# Legacy diagnostic sensor names removed in favour of MQTT Connection attributes
_LEGACY_DIAGNOSTIC_SENSOR_NAMES = ("Last MQTT Sent", "Last MQTT Received", "Last HTTP Request")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember sensors from a config entry."""
    data = entry.runtime_data
    selected_gateway_id = entry.data.get(CONF_GATEWAY_ID)

    # Remove legacy timestamp sensors (now attributes on MQTT Connection) to avoid duplicates
    registry = er.async_get(hass)
    for name in _LEGACY_DIAGNOSTIC_SENSOR_NAMES:
        uid = f"{entry.entry_id}_{name}"
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, uid)
        if entity_id:
            registry.async_remove(entity_id)

    entities: list[SensorEntity] = [
        # Diagnostic sensor for main device (timestamps as attributes to avoid logbook noise)
        EphemberMQTTConnectionSensor(data, entry),
    ]

    # Build zone list from cached HTTP snapshot (populated at integration setup)
    homes = data.last_http_zones_data or []
    
    # Filter to selected home if gateway_id is specified
    if selected_gateway_id:
        homes = [home for home in homes if home.get("gatewayid") == selected_gateway_id]
    
    zones: list[dict[str, Any]] = [
        zone for home in homes for zone in home.get("zones", [])
    ]

    # Per-zone heating sensors (attached to the same zone devices as the climate entities)
    for zone in zones:
        zid = zone.get("zoneid")
        if not zid:
            continue
        sensor = EphemberZoneHeatingSensor(data, entry, zone)
        entities.append(sensor)
        data.zone_id_to_heating_sensor[zid] = sensor

        # Ensure cache has a value (used for system sensor & startup)
        if zid not in data.zone_heating:
            data.zone_heating[zid] = _zone_is_heating(zone)

    # System-wide heating sensor (attached to main EPH Controls Ember device)
    system_sensor = EphemberSystemHeatingSensor(data, entry)
    data.system_heating_sensor = system_sensor
    entities.append(system_sensor)

    # Daily heating duration sensor (tracks heating time per day)
    duration_sensor = EphemberHeatingDurationSensor(data, entry)
    entities.append(duration_sensor)

    # Gas consumption sensor (tracks cumulative gas consumption)
    gas_consumption_sensor = EphemberGasConsumptionSensor(data, entry)
    entities.append(gas_consumption_sensor)

    async_add_entities(entities)


# -------------------------
# HEATING SENSORS (push updated via MQTT)
# -------------------------

class EphemberZoneHeatingSensor(SensorEntity, RestoreEntity):
    """Sensor that exposes per-zone heating state ('idle' or 'heating')."""

    _attr_has_entity_name = True
    _attr_name = "Heating"
    _attr_icon = "mdi:radiator"
    _attr_should_poll = False

    def __init__(self, data: Any, entry: EphemberConfigEntry, zone: dict[str, Any]) -> None:
        self._data = data
        self._entry = entry
        self._zone_id: str = zone.get("zoneid")
        self._zone_name: str = zone_name(zone)

        self._attr_unique_id = f"{entry.entry_id}_{self._zone_id}_heating"

        # Attach to the SAME device as the climate entity for this zone
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=self._zone_name,
            manufacturer="EPH Controls",
        )

        self._state: str = "idle"  # Initialize with default state

    async def async_added_to_hass(self) -> None:
        """Restore last state so we don't wait for MQTT."""
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
            state_restored = False
            if last and last.state not in (None, "unknown", "unavailable"):
                self._state = last.state
                state_restored = True

            # If not restored, initialize from cache (populated at integration startup)
            if not state_restored:
                if self._data and hasattr(self._data, 'zone_heating'):
                    self._state = "heating" if self._data.zone_heating.get(self._zone_id, False) else "idle"

            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error(
                "Error initializing zone heating sensor %s: %s", self._zone_id, err, exc_info=True
            )
            self._state = "idle"
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        return True

    @property
    def native_value(self) -> str:
        """Return the sensor state."""
        return self._state

    @callback
    def handle_zone_update(self, zone: dict[str, Any]) -> None:
        """Handle a zone update (called from MQTT callback via call_soon_threadsafe)."""
        heating = _zone_is_heating(zone)
        self._data.zone_heating[self._zone_id] = heating
        self._state = "heating" if heating else "idle"
        self.async_write_ha_state()


class EphemberSystemHeatingSensor(SensorEntity, RestoreEntity):
    """Sensor that exposes overall heating state ('idle' or 'heating')."""

    _attr_has_entity_name = True
    _attr_name = "Heating"
    _attr_icon = "mdi:fire"
    _attr_should_poll = False

    def __init__(self, data: Any, entry: EphemberConfigEntry) -> None:
        self._data = data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_system_heating"

        # Attach to the main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EPH Controls Ember",
            manufacturer="EPH Controls",
            model=_build_device_model_string(data),
        )

        self._state: str = "idle"  # Initialize with default state

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
            state_restored = False
            if last and last.state not in (None, "unknown", "unavailable"):
                self._state = last.state
                state_restored = True

            # If not restored, initialize from cache (populated at integration startup)
            if not state_restored:
                if self._data and hasattr(self._data, 'zone_heating') and self._data.zone_heating:
                    self._state = "heating" if any(self._data.zone_heating.values()) else "idle"

            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error(
                "Error initializing system heating sensor: %s", err, exc_info=True
            )
            self._state = "idle"
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        return True

    @property
    def native_value(self) -> str:
        """Return the sensor state."""
        return self._state

    @callback
    def handle_system_update(self) -> None:
        """Recompute overall heating based on cached per-zone heating flags."""
        try:
            if not self._data or not hasattr(self._data, 'zone_heating'):
                _LOGGER.warning("System heating sensor: _data.zone_heating not available")
                return

            heating_zones = [zid for zid, is_heating in self._data.zone_heating.items() if is_heating]
            new_state = "heating" if heating_zones else "idle"
            _LOGGER.debug(
                "System heating sensor update: zones=%s, heating_zones=%s, old_state=%s, new_state=%s",
                list(self._data.zone_heating.keys()),
                heating_zones,
                self._state,
                new_state,
            )
            self._state = new_state
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error(
                "Error updating system heating sensor: %s", err, exc_info=True
            )


class EphemberHeatingDurationSensor(SensorEntity, RestoreEntity):
    """Sensor that tracks daily heating duration in hours."""

    _attr_has_entity_name = True
    _attr_name = "Heating Duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:clock-outline"
    _attr_should_poll = False

    def __init__(self, data: Any, entry: EphemberConfigEntry) -> None:
        """Initialize the heating duration sensor."""
        self._data = data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_heating_duration"

        # Attach to the main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EPH Controls Ember",
            manufacturer="EPH Controls",
            model=_build_device_model_string(data),
        )

        # Track accumulated time in hours
        self._accumulated_hours: float = 0.0
        # Track when heating state started (None if not heating)
        self._heating_start_time: datetime | None = None
        # Track current heating state
        self._is_heating: bool = False
        # Entity ID of the system heating sensor
        self._heating_sensor_entity_id: str | None = None
        # Store callbacks for cleanup
        self._unsub_state_change: Callable[[], None] | None = None
        self._unsub_midnight_reset: Callable[[], None] | None = None
        self._unsub_periodic_update: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Set up listeners and restore state."""
        await super().async_added_to_hass()

        try:
            # Restore accumulated time from previous state
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                try:
                    self._accumulated_hours = float(last.state)
                except (ValueError, TypeError):
                    self._accumulated_hours = 0.0

            # Find the system heating sensor entity_id via entity registry
            entity_registry = er.async_get(self.hass)
            system_heating_unique_id = f"{self._entry.entry_id}_system_heating"
            entity_entry = entity_registry.async_get_entity_id(
                "sensor", DOMAIN, system_heating_unique_id
            )

            if entity_entry is None:
                _LOGGER.warning(
                    "System heating sensor entity not found for unique_id: %s",
                    system_heating_unique_id,
                )
                return

            self._heating_sensor_entity_id = entity_entry

            # Get current state of heating sensor
            current_state = self.hass.states.get(self._heating_sensor_entity_id)
            if current_state:
                self._is_heating = current_state.state == "heating"
                if self._is_heating:
                    # If heating, record start time (use state's last_changed if available)
                    self._heating_start_time = current_state.last_changed
                    # Start periodic updates
                    self._start_periodic_update()

            # Set up state change listener
            self._unsub_state_change = async_track_state_change_event(
                self.hass,
                [self._heating_sensor_entity_id],
                self._handle_heating_state_change,
            )

            # Schedule midnight reset
            self._schedule_midnight_reset()

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(
                "Error initializing heating duration sensor: %s", err, exc_info=True
            )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners."""
        if self._unsub_state_change:
            self._unsub_state_change()
        if self._unsub_midnight_reset:
            self._unsub_midnight_reset()
        if self._unsub_periodic_update:
            self._unsub_periodic_update()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_heating_state_change(self, event) -> None:
        """Handle state changes in the heating sensor."""
        try:
            new_state = event.data.get("new_state")
            if new_state is None:
                return

            new_is_heating = new_state.state == "heating"
            state_changed_time = new_state.last_changed

            # If transitioning from heating to not heating, accumulate time
            if self._is_heating and not new_is_heating:
                if self._heating_start_time:
                    elapsed = (state_changed_time - self._heating_start_time).total_seconds()
                    self._accumulated_hours += elapsed / 3600.0
                    self._heating_start_time = None
                self._is_heating = False
                # Stop periodic updates
                if self._unsub_periodic_update:
                    self._unsub_periodic_update()
                    self._unsub_periodic_update = None

            # If transitioning to heating, record start time
            elif not self._is_heating and new_is_heating:
                self._heating_start_time = state_changed_time
                self._is_heating = True
                # Start periodic updates (every minute)
                self._start_periodic_update()

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(
                "Error handling heating state change: %s", err, exc_info=True
            )

    @callback
    def _handle_midnight_reset(self, now: datetime) -> None:
        """Reset accumulated time at midnight."""
        try:
            # Reset accumulated hours to 0 for the new day
            self._accumulated_hours = 0.0

            # If currently heating, reset start time to midnight to track from new day
            if self._is_heating:
                self._heating_start_time = now

            self.async_write_ha_state()

            # Schedule next midnight reset
            self._schedule_midnight_reset()

        except Exception as err:
            _LOGGER.error("Error in midnight reset: %s", err, exc_info=True)

    def _schedule_midnight_reset(self) -> None:
        """Schedule the next midnight reset."""
        try:
            next_midnight = dt_util.start_of_local_day(
                dt_util.now() + timedelta(days=1)
            )
            self._unsub_midnight_reset = async_track_point_in_time(
                self.hass, self._handle_midnight_reset, next_midnight
            )
        except Exception as err:
            _LOGGER.error("Error scheduling midnight reset: %s", err, exc_info=True)

    def _start_periodic_update(self) -> None:
        """Start periodic updates (every minute) to gradually increase sensor value."""
        try:
            # Cancel existing periodic update if any
            if self._unsub_periodic_update:
                self._unsub_periodic_update()
            
            @callback
            def _periodic_update_callback(now: datetime) -> None:
                """Update sensor state periodically."""
                self.async_write_ha_state()

            # Schedule updates every minute
            self._unsub_periodic_update = async_track_time_interval(
                self.hass,
                _periodic_update_callback,
                timedelta(minutes=1),
            )
        except Exception as err:
            _LOGGER.error("Error starting periodic update: %s", err, exc_info=True)

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        if self._heating_sensor_entity_id:
            state = self.hass.states.get(self._heating_sensor_entity_id)
            return state is not None and state.state not in ("unavailable", "unknown")
        return False

    @property
    def native_value(self) -> float:
        """Return the accumulated heating duration in hours."""
        # If currently heating, add elapsed time since start
        if self._is_heating and self._heating_start_time:
            now = dt_util.utcnow()
            elapsed = (now - self._heating_start_time).total_seconds()
            return self._accumulated_hours + (elapsed / 3600.0)
        return self._accumulated_hours


class EphemberGasConsumptionSensor(SensorEntity, RestoreEntity):
    """Sensor that tracks cumulative gas consumption in m³."""

    _attr_has_entity_name = True
    _attr_name = "Gas Consumption"
    _attr_device_class = SensorDeviceClass.GAS
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:fire"
    _attr_should_poll = False

    def __init__(self, data: Any, entry: EphemberConfigEntry) -> None:
        """Initialize the gas consumption sensor."""
        self._data = data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_gas_consumption"

        # Get gas consumption rate from config (default 1.5 m³/hour)
        self._gas_rate_per_hour: float = entry.options.get(CONF_GAS_CONSUMPTION_RATE, 1.5)

        # Attach to the main device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EPH Controls Ember",
            manufacturer="EPH Controls",
            model=_build_device_model_string(data),
        )

        # Track cumulative gas consumption in m³ (never resets)
        self._cumulative_consumption: float = 0.0
        # Track last known duration value to detect changes
        self._last_duration_hours: float = 0.0
        # Entity ID of the heating duration sensor
        self._duration_sensor_entity_id: str | None = None
        # Entity ID of the system heating sensor (to detect when heating stops)
        self._system_heating_sensor_entity_id: str | None = None
        # Store callbacks for cleanup
        self._unsub_state_change: Callable[[], None] | None = None
        self._unsub_heating_state_change: Callable[[], None] | None = None
        self._unsub_periodic_update: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Set up listeners and restore state."""
        await super().async_added_to_hass()

        try:
            # Restore cumulative consumption from previous state
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unknown", "unavailable"):
                try:
                    self._cumulative_consumption = float(last.state)
                except (ValueError, TypeError):
                    self._cumulative_consumption = 0.0

            # Find the heating duration sensor entity_id via entity registry
            entity_registry = er.async_get(self.hass)
            duration_unique_id = f"{self._entry.entry_id}_heating_duration"
            entity_entry = entity_registry.async_get_entity_id(
                "sensor", DOMAIN, duration_unique_id
            )

            if entity_entry is None:
                _LOGGER.warning(
                    "Heating duration sensor entity not found for unique_id: %s",
                    duration_unique_id,
                )
                return

            self._duration_sensor_entity_id = entity_entry

            # Get current state of duration sensor
            current_state = self.hass.states.get(self._duration_sensor_entity_id)
            if current_state and current_state.state not in (None, "unknown", "unavailable"):
                try:
                    self._last_duration_hours = float(current_state.state)
                except (ValueError, TypeError):
                    self._last_duration_hours = 0.0

            # Set up listener for system heating sensor (to start/stop periodic updates)
            system_heating_unique_id = f"{self._entry.entry_id}_system_heating"
            system_heating_entity_id = entity_registry.async_get_entity_id(
                "sensor", DOMAIN, system_heating_unique_id
            )
            if system_heating_entity_id:
                self._system_heating_sensor_entity_id = system_heating_entity_id
                system_heating_state = self.hass.states.get(system_heating_entity_id)
                if system_heating_state and system_heating_state.state == "heating":
                    # Start periodic updates if heating is active
                    self._start_periodic_update()

                # Set up state change listener for system heating sensor
                self._unsub_heating_state_change = async_track_state_change_event(
                    self.hass,
                    [system_heating_entity_id],
                    self._handle_heating_state_change,
                )

            # Set up state change listener for duration sensor
            self._unsub_state_change = async_track_state_change_event(
                self.hass,
                [self._duration_sensor_entity_id],
                self._handle_duration_state_change,
            )

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(
                "Error initializing gas consumption sensor: %s", err, exc_info=True
            )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up listeners."""
        if self._unsub_state_change:
            self._unsub_state_change()
        if self._unsub_heating_state_change:
            self._unsub_heating_state_change()
        if self._unsub_periodic_update:
            self._unsub_periodic_update()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_heating_state_change(self, event) -> None:
        """Handle state changes in the system heating sensor."""
        try:
            new_state = event.data.get("new_state")
            if new_state is None:
                return

            is_heating = new_state.state == "heating"

            if is_heating:
                # Start periodic updates when heating starts
                if not self._unsub_periodic_update:
                    self._start_periodic_update()
            else:
                # Stop periodic updates when heating stops
                if self._unsub_periodic_update:
                    self._unsub_periodic_update()
                    self._unsub_periodic_update = None

        except Exception as err:
            _LOGGER.error(
                "Error handling heating state change: %s", err, exc_info=True
            )

    @callback
    def _handle_duration_state_change(self, event) -> None:
        """Handle state changes in the heating duration sensor."""
        try:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in (None, "unknown", "unavailable"):
                return

            try:
                new_duration_hours = float(new_state.state)
            except (ValueError, TypeError):
                return

            # Calculate the difference in duration
            duration_delta = new_duration_hours - self._last_duration_hours

            # Only update if duration increased (heating is active)
            if duration_delta > 0:
                # Calculate gas consumption for this duration increase
                consumption_delta = duration_delta * self._gas_rate_per_hour
                self._cumulative_consumption += consumption_delta
                self._last_duration_hours = new_duration_hours

                # Start periodic updates if not already running
                if not self._unsub_periodic_update:
                    self._start_periodic_update()

            # If duration decreased (midnight reset), don't change cumulative consumption
            # but update last_duration_hours to track from new baseline
            elif duration_delta < 0:
                self._last_duration_hours = new_duration_hours

            self.async_write_ha_state()

        except Exception as err:
            _LOGGER.error(
                "Error handling duration state change: %s", err, exc_info=True
            )

    def _start_periodic_update(self) -> None:
        """Start periodic updates (every minute) to continuously update consumption."""
        try:
            # Cancel existing periodic update if any
            if self._unsub_periodic_update:
                self._unsub_periodic_update()

            @callback
            def _periodic_update_callback(now: datetime) -> None:
                """Update gas consumption periodically based on current duration."""
                if not self._duration_sensor_entity_id:
                    return

                duration_state = self.hass.states.get(self._duration_sensor_entity_id)
                if not duration_state or duration_state.state in (None, "unknown", "unavailable"):
                    return

                try:
                    current_duration_hours = float(duration_state.state)
                except (ValueError, TypeError):
                    return

                # Calculate delta from last known duration
                duration_delta = current_duration_hours - self._last_duration_hours

                # Only update if duration increased (heating is active)
                if duration_delta > 0:
                    # Calculate gas consumption for this duration increase
                    consumption_delta = duration_delta * self._gas_rate_per_hour
                    self._cumulative_consumption += consumption_delta
                    self._last_duration_hours = current_duration_hours
                    self.async_write_ha_state()
                elif duration_delta < 0:
                    # Duration reset (midnight), update baseline without changing cumulative
                    self._last_duration_hours = current_duration_hours

            # Schedule updates every minute
            self._unsub_periodic_update = async_track_time_interval(
                self.hass,
                _periodic_update_callback,
                timedelta(minutes=1),
            )
        except Exception as err:
            _LOGGER.error("Error starting periodic update: %s", err, exc_info=True)

    @property
    def available(self) -> bool:
        """Return if the entity is available."""
        if self._duration_sensor_entity_id:
            state = self.hass.states.get(self._duration_sensor_entity_id)
            return state is not None and state.state not in ("unavailable", "unknown")
        return False

    @property
    def native_value(self) -> float:
        """Return the cumulative gas consumption in m³."""
        # Return cumulative consumption (never resets, continuously increasing)
        return self._cumulative_consumption


# -------------------------
# EXISTING DIAGNOSTIC SENSORS
# -------------------------

class EphemberDiagnosticSensor(SensorEntity):
    """Base class for EPH Controls Ember diagnostic sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, data: Any, entry: EphemberConfigEntry) -> None:
        self._data = data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EPH Controls Ember",
            manufacturer="EPH Controls",
            model=_build_device_model_string(data) if data else None,
        )

    @property
    def native_value(self) -> str | None:
        raise NotImplementedError


class EphemberMQTTConnectionSensor(EphemberDiagnosticSensor):
    """Sensor for MQTT connection status.

    Diagnostic timestamps (last MQTT sent/received, last HTTP request) are exposed
    as extra_state_attributes to avoid logbook noise. Attribute-only updates do
    not create history entries.
    """

    _attr_name = "MQTT Connection"
    _attr_icon = "mdi:connection"

    @property
    def native_value(self) -> str:
        if self._data and self._data.ember:
            if hasattr(self._data.ember, "is_mqtt_connected"):
                self._data.mqtt_connected = self._data.ember.is_mqtt_connected()
            return "connected" if self._data.mqtt_connected else "disconnected"
        return "disconnected"

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        """Diagnostic timestamps (attributes only; no logbook entries)."""
        if not self._data:
            return {}
        d = self._data
        return {
            "last_mqtt_sent": d.last_mqtt_sent.isoformat() if d.last_mqtt_sent else None,
            "last_mqtt_received": d.last_mqtt_received.isoformat() if d.last_mqtt_received else None,
            "last_http_request": d.last_http_request.isoformat() if d.last_http_request else None,
        }
