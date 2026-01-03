"""Support for EPH Controls Ember sensors (diagnostics + heating state)."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EphemberConfigEntry
from .const import DOMAIN, EPHBoilerStates
from .pyephember2.pyephember2 import boiler_state, zone_name

_LOGGER = logging.getLogger(__name__)


def _zone_is_heating(zone: dict[str, Any]) -> bool:
    """Return True if zone boiler_state reports ON."""
    try:
        return boiler_state(zone) == EPHBoilerStates.ON
    except Exception:
        return False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember sensors from a config entry."""
    data = entry.runtime_data

    entities: list[SensorEntity] = [
        # Diagnostic sensors for main device
        EphemberMQTTConnectionSensor(data, entry),
        EphemberMQTTSentSensor(data, entry),
        EphemberMQTTReceivedSensor(data, entry),
        EphemberHTTPRequestSensor(data, entry),
    ]

    # Build zone list from cached HTTP snapshot (populated at integration setup)
    homes = data.last_http_zones_data or []
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
            model=data.system_type if getattr(data, "system_type", None) else None,
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
            model=data.system_type if data and data.system_type else None,
        )

    @property
    def native_value(self) -> str | None:
        raise NotImplementedError


class EphemberMQTTConnectionSensor(EphemberDiagnosticSensor):
    """Sensor for MQTT connection status."""

    _attr_name = "MQTT Connection"
    _attr_icon = "mdi:connection"

    @property
    def native_value(self) -> str:
        if self._data and self._data.ember:
            if hasattr(self._data.ember, "is_mqtt_connected"):
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
        return self._data.last_mqtt_sent if self._data and self._data.last_mqtt_sent else None


class EphemberMQTTReceivedSensor(EphemberDiagnosticSensor):
    """Sensor for last MQTT message received timestamp."""

    _attr_name = "Last MQTT Received"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:download"

    @property
    def native_value(self) -> datetime | None:
        return self._data.last_mqtt_received if self._data and self._data.last_mqtt_received else None


class EphemberHTTPRequestSensor(EphemberDiagnosticSensor):
    """Sensor for last HTTP request timestamp."""

    _attr_name = "Last HTTP Request"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:web"

    @property
    def native_value(self) -> datetime | None:
        return self._data.last_http_request if self._data and self._data.last_http_request else None
