"""Support for EPH Controls Ember diagnostic sensors."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, CoreState
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import EphemberConfigEntry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember diagnostic sensors from a config entry."""
    data = entry.runtime_data
    
    # Diagnostic sensors for main device
    entities = [
        EphemberMQTTConnectionSensor(data, entry),
        EphemberMQTTSentSensor(data, entry),
        EphemberMQTTReceivedSensor(data, entry),
        EphemberHTTPRequestSensor(data, entry),
        EphemberAggregateHeatingSensor(data, entry),
    ]
    
    # Add per-zone heating sensors
    # Get zone IDs from cached zones data or climate entities
    zone_ids = set()
    # Try to get from cached HTTP data first (most reliable during setup)
    if hasattr(data, 'last_http_zones_data') and data.last_http_zones_data:
        for home in data.last_http_zones_data:
            for zone in home.get("zones", []):
                zone_id = zone.get("zoneid")
                if zone_id:
                    zone_ids.add(zone_id)
    # Also check if climate entities are already registered
    if hasattr(data, 'zone_id_to_entity') and data.zone_id_to_entity:
        zone_ids.update(data.zone_id_to_entity.keys())
    
    # Create sensors for all discovered zone IDs
    for zone_id in zone_ids:
        entities.append(EphemberZoneHeatingSensor(data, entry, zone_id))
    
    async_add_entities(entities)


class EphemberDiagnosticSensor(SensorEntity):
    """Base class for EPH Controls Ember diagnostic sensors."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(self, data: Any, entry: EphemberConfigEntry) -> None:
        """Initialize the sensor."""
        self._data = data
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{self._attr_name}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="EPH Controls Ember",
            manufacturer="EPH Controls",
            model=data.system_type if data and data.system_type else None,
        )

    @property
    def native_value(self) -> str | None:
        """Return the state of the sensor."""
        raise NotImplementedError


class EphemberMQTTConnectionSensor(EphemberDiagnosticSensor):
    """Sensor for MQTT connection status."""

    _attr_name = "MQTT Connection"
    _attr_icon = "mdi:connection"

    @property
    def native_value(self) -> str:
        """Return the MQTT connection status."""
        if self._data and self._data.ember:
            # Update connection status from ember if available
            if hasattr(self._data.ember, 'is_mqtt_connected'):
                self._data.mqtt_connected = self._data.ember.is_mqtt_connected()
            return "connected" if self._data.mqtt_connected else "disconnected"
        return "disconnected"


class EphemberMQTTSentSensor(EphemberDiagnosticSensor):
    """Sensor for last MQTT message sent timestamp."""

    _attr_name = "Last MQTT Sent"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:send"

    @property
    def native_value(self) -> datetime | None:
        """Return the last MQTT message sent timestamp."""
        if self._data and self._data.last_mqtt_sent:
            return self._data.last_mqtt_sent
        return None


class EphemberMQTTReceivedSensor(EphemberDiagnosticSensor):
    """Sensor for last MQTT message received timestamp."""

    _attr_name = "Last MQTT Received"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:download"

    @property
    def native_value(self) -> datetime | None:
        """Return the last MQTT message received timestamp."""
        if self._data and self._data.last_mqtt_received:
            return self._data.last_mqtt_received
        return None


class EphemberHTTPRequestSensor(EphemberDiagnosticSensor):
    """Sensor for last HTTP request timestamp."""

    _attr_name = "Last HTTP Request"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:web"

    @property
    def native_value(self) -> datetime | None:
        """Return the last HTTP request timestamp."""
        if self._data and self._data.last_http_request:
            return self._data.last_http_request
        return None


class EphemberZoneHeatingSensor(SensorEntity):
    """Sensor for individual zone heating state."""

    _attr_has_entity_name = True
    _attr_should_poll = False  # Will update when climate entity updates
    _attr_icon = "mdi:radiator"

    def __init__(self, data: Any, entry: EphemberConfigEntry, zone_id: str):
        """Initialize the zone heating sensor."""
        self._data = data
        self._entry = entry
        self._zone_id = zone_id
        self._attr_name = "Heating"
        self._attr_unique_id = f"{entry.entry_id}_{zone_id}_heating"
        
        # Associate with the zone's device (same as climate entity)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, zone_id)},
        )
        
        # Store entity_id (will be set when added to hass)
        self._climate_entity_id: str | None = None
        self._listener_remover: Callable | None = None

    def _setup_listener(self) -> None:
        """Set up state change listener for climate entity."""
        if self._climate_entity_id and hasattr(self, 'hass') and self.hass:
            # Remove any existing listener first
            if self._listener_remover:
                self._listener_remover()
            
            # Set up new listener
            self._listener_remover = self.async_on_remove(
                self.hass.helpers.event.async_track_state_change_event(
                    self._climate_entity_id,
                    self._async_climate_state_changed,
                )
            )
            _LOGGER.debug(
                "Zone heating sensor %s: Set up listener for entity_id=%s",
                self._zone_id,
                self._climate_entity_id,
            )

    @property
    def native_value(self) -> str:
        """Return 'heating' or 'idle'."""
        # Try to get entity_id if not set (handles late registration)
        if not self._climate_entity_id and self._data and self._data.zone_id_to_entity:
            climate_entity = self._data.zone_id_to_entity.get(self._zone_id)
            if climate_entity and hasattr(climate_entity, 'entity_id'):
                self._climate_entity_id = climate_entity.entity_id
                _LOGGER.debug(
                    "Zone heating sensor %s: Found entity_id=%s in property, setting up listener",
                    self._zone_id,
                    self._climate_entity_id,
                )
                # Set up listener now that we have entity_id
                self._setup_listener()
        
        # Read hvac_action from state machine (like template sensor does)
        if self._climate_entity_id and hasattr(self, 'hass') and self.hass:
            # Check if hass is actually running
            if self.hass.state == CoreState.running:
                state = self.hass.states.get(self._climate_entity_id)
                if state:
                    hvac_action = state.attributes.get('hvac_action')
                    # DEBUG: Log what we're getting (only log occasionally to avoid spam)
                    if not hasattr(self, '_last_logged_action') or self._last_logged_action != hvac_action:
                        _LOGGER.debug(
                            "Zone heating sensor %s: entity_id=%s, hvac_action=%s",
                            self._zone_id,
                            self._climate_entity_id,
                            hvac_action,
                        )
                        self._last_logged_action = hvac_action
                    if hvac_action == 'heating':
                        return "heating"
        
        return "idle"

    async def async_added_to_hass(self) -> None:
        """Subscribe to climate entity updates."""
        await super().async_added_to_hass()
        
        # Get reference to climate entity and its entity_id
        climate_entity = self._data.zone_id_to_entity.get(self._zone_id) if self._data else None
        if climate_entity:
            self._climate_entity_id = climate_entity.entity_id
            self._setup_listener()
        else:
            _LOGGER.debug(
                "Zone heating sensor %s: Climate entity not found during async_added_to_hass, will retry",
                self._zone_id,
            )
            # Schedule a retry after a short delay (climate entities may be registered later)
            @callback
            def retry_setup(_now):
                """Retry finding the climate entity after delay."""
                if self._zone_id in self._data.zone_id_to_entity:
                    climate_entity = self._data.zone_id_to_entity[self._zone_id]
                    if climate_entity:
                        self._climate_entity_id = climate_entity.entity_id
                        self._setup_listener()
                        # Trigger an update
                        self.async_write_ha_state()
                        _LOGGER.debug(
                            "Zone heating sensor %s: Found climate entity on retry, entity_id=%s",
                            self._zone_id,
                            self._climate_entity_id,
                        )
            
            # Retry after 2 seconds (gives time for climate platform to register entities)
            self.async_on_remove(
                async_call_later(self.hass, timedelta(seconds=2.0), retry_setup)
            )

    @callback
    def _async_climate_state_changed(self, event) -> None:
        """Handle climate entity state changes."""
        self.async_write_ha_state()


class EphemberAggregateHeatingSensor(EphemberDiagnosticSensor):
    """Sensor for aggregate heating state across all zones."""

    _attr_name = "Heating"
    _attr_icon = "mdi:radiator"
    _attr_should_poll = False  # Will update when zone entities update

    def __init__(self, data: Any, entry: EphemberConfigEntry):
        """Initialize the aggregate heating sensor."""
        super().__init__(data, entry)
        self._entry = entry
        # Track zone entity IDs for state change listening
        self._zone_entity_ids: list[str] = []

    @property
    def native_value(self) -> str:
        """Return 'heating' if any zone is heating, 'idle' if all are idle."""
        if not self._data or not self._data.zone_id_to_entity:
            return "idle"

        # Check all climate entities via state machine
        if hasattr(self, 'hass') and self.hass and self.hass.state == CoreState.running:
            for zone_id, climate_entity in self._data.zone_id_to_entity.items():
                if climate_entity and hasattr(climate_entity, 'entity_id'):
                    entity_id = climate_entity.entity_id
                    state = self.hass.states.get(entity_id)
                    if state:
                        hvac_action = state.attributes.get('hvac_action')
                        _LOGGER.debug(
                            "Aggregate heating sensor: zone_id=%s, entity_id=%s, hvac_action=%s",
                            zone_id,
                            entity_id,
                            hvac_action,
                        )
                        if hvac_action == 'heating':
                            return "heating"
                    else:
                        _LOGGER.debug(
                            "Aggregate heating sensor: state is None for entity_id=%s",
                            entity_id,
                        )
        return "idle"

    async def async_added_to_hass(self) -> None:
        """Subscribe to all zone entity updates."""
        await super().async_added_to_hass()

        # Get all zone entity IDs
        if self._data and self._data.zone_id_to_entity:
            self._zone_entity_ids = [
                entity.entity_id
                for entity in self._data.zone_id_to_entity.values()
                if entity and hasattr(entity, 'entity_id')
            ]

        # Listen for state changes of all climate entities
        if self._zone_entity_ids:
            self.async_on_remove(
                self.hass.helpers.event.async_track_state_change_event(
                    self._zone_entity_ids,
                    self._async_zone_state_changed,
                )
            )

    @callback
    def _async_zone_state_changed(self, event) -> None:
        """Handle zone entity state changes."""
        self.async_write_ha_state()
