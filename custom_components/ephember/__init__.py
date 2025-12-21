"""The EPH Controls Ember integration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .pyephember2.pyephember2 import EphEmber
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.CLIMATE, Platform.SENSOR, Platform.SWITCH]

if TYPE_CHECKING:
    from collections.abc import Callable

type EphemberConfigEntry = ConfigEntry[EphemberData]


class EphemberData:
    """Store runtime data for the integration."""

    def __init__(self, ember: EphEmber) -> None:
        """Initialize data storage."""
        self.ember = ember
        self.mac_to_zone_id: dict[str, str] = {}
        self.zone_id_to_entity: dict[str, Any] = {}
        self.zone_id_to_switch: dict[str, Any] = {}
        self.last_mqtt_sent: datetime | None = None
        self.last_mqtt_received: datetime | None = None
        self.last_http_request: datetime | None = None
        self.mqtt_connected: bool = False
        self.system_type: str | None = None


async def async_setup_entry(hass: HomeAssistant, entry: EphemberConfigEntry) -> bool:
    """Set up EPH Controls Ember from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    try:
        ember = await hass.async_add_executor_job(EphEmber, username, password)
    except RuntimeError as err:
        raise ConfigEntryNotReady(f"Unable to connect to EPH Controls: {err}") from err

    # Get zones to build MAC to zone_id mapping
    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError as err:
        raise ConfigEntryNotReady(f"Unable to get zones from EPH Controls: {err}") from err

    # Create data storage
    data = EphemberData(ember)

    # Update HTTP request timestamp for initial request
    data.last_http_request = datetime.now(timezone.utc)
    
    # Build MAC to zone_id mapping
    for home in homes:
        for zone in home.get("zones", []):
            mac = zone.get("mac")
            zone_id = zone.get("zoneid")
            if mac and zone_id:
                data.mac_to_zone_id[mac] = zone_id
            # Extract systemType from first zone (all zones typically have same systemType)
            if data.system_type is None and zone.get("systemType"):
                data.system_type = zone.get("systemType")

    # Set up MQTT callbacks
    def on_mqtt_pointdata(mac: str, parsed_pointdata: dict) -> None:
        """Handle MQTT pointdata updates."""
        data.last_mqtt_received = datetime.now(timezone.utc)
        data.mqtt_connected = True
        
        zone_id = data.mac_to_zone_id.get(mac)
        if zone_id and zone_id in data.zone_id_to_entity:
            entity = data.zone_id_to_entity[zone_id]
            # Update zone data from MQTT
            zone = ember.get_zone_by_mac(mac)
            if zone:
                entity._zone = zone
                # Schedule state update on event loop (thread-safe)
                # This callback runs in MQTT thread, so we need to schedule on event loop
                # async_write_ha_state() is a @callback method (synchronous but must run on event loop)
                hass.loop.call_soon_threadsafe(entity.async_write_ha_state)
        _LOGGER.debug("MQTT update received for MAC %s (zone_id: %s)", mac, zone_id)

    def on_mqtt_log(direction: str, content: str) -> None:
        """Handle MQTT log messages to track sent messages."""
        if direction == "SEND":
            data.last_mqtt_sent = datetime.now(timezone.utc)
            data.mqtt_connected = True

    def on_mqtt_connect(client, userdata, flags, rc, properties=None) -> None:
        """Handle MQTT connection."""
        data.mqtt_connected = True
        _LOGGER.info("MQTT connected")

    def on_mqtt_disconnect(client, userdata, rc, properties=None) -> None:
        """Handle MQTT disconnection."""
        data.mqtt_connected = False
        _LOGGER.warning("MQTT disconnected (rc: %s)", rc)

    # Set up MQTT callbacks
    ember.set_mqtt_pointdata_callback(on_mqtt_pointdata)
    ember.set_mqtt_log_callback(on_mqtt_log)
    
    # Start MQTT listener
    try:
        await hass.async_add_executor_job(ember.start_mqtt_listener, None)
        # Check connection status after starting
        data.mqtt_connected = ember.is_mqtt_connected() if hasattr(ember, 'is_mqtt_connected') else False
        _LOGGER.info("MQTT listener started (connected: %s)", data.mqtt_connected)
    except Exception as err:
        _LOGGER.warning("Failed to start MQTT listener: %s. Continuing with HTTP-only mode.", err)
        data.mqtt_connected = False
        # Continue without MQTT - HTTP polling will still work

    # Store data in runtime_data
    entry.runtime_data = data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: EphemberConfigEntry) -> bool:
    """Unload a config entry."""
    data = entry.runtime_data
    if data and data.ember:
        # Stop MQTT listener
        try:
            await hass.async_add_executor_job(data.ember.stop_mqtt_listener)
            _LOGGER.info("MQTT listener stopped")
        except Exception as err:
            _LOGGER.warning("Error stopping MQTT listener: %s", err)
    
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
