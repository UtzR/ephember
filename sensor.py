"""Support for EPH Controls Ember diagnostic sensors."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

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
    
    entities = [
        EphemberMQTTConnectionSensor(data, entry),
        EphemberMQTTSentSensor(data, entry),
        EphemberMQTTReceivedSensor(data, entry),
        EphemberHTTPRequestSensor(data, entry),
    ]
    
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
