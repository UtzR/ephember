"""The EPH Controls Ember integration."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING, Any

from .pyephember2.pyephember2 import EphEmber, decode_point_data, boiler_state
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, EPHBoilerStates

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
        # Heating sensors (push-updated via MQTT)
        self.zone_id_to_heating_sensor: dict[str, Any] = {}
        self.system_heating_sensor: Any | None = None
        # Cached heating state per zone_id (True=heating)
        self.zone_heating: dict[str, bool] = {}
        self.last_mqtt_sent: datetime | None = None
        self.last_mqtt_received: datetime | None = None
        self.last_http_request: datetime | None = None
        self.mqtt_connected: bool = False
        self.system_type: str | None = None
        # Track last 5 MQTT messages received and sent
        self.recent_mqtt_messages_received: deque = deque(maxlen=5)
        self.recent_mqtt_messages_sent: deque = deque(maxlen=5)
        # Track last HTTP zones data (list of homes, each containing zones)
        self.last_http_zones_data: list[dict[str, Any]] | None = None
        # Cache for raw MQTT messages (mac -> {topic, raw_payload})
        self._raw_mqtt_message_cache: dict[str, dict[str, Any]] = {}


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
    # Store HTTP zones data
    data.last_http_zones_data = homes

    # Initialize cached zone heating state from HTTP snapshot
    for home in homes:
        for zone in home.get("zones", []):
            zid = zone.get("zoneid")
            if zid:
                try:
                    data.zone_heating[zid] = (boiler_state(zone) == EPHBoilerStates.ON)
                except Exception:
                    data.zone_heating[zid] = False

    
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

    # Create main device in device registry
    device_registry = dr.async_get(hass)
    main_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="EPH Controls Ember",
        manufacturer="EPH Controls",
        model=data.system_type if data.system_type else "Ember System",
    )

    # Set up MQTT callbacks
    def on_mqtt_message(topic: str, msg_dict: dict[str, Any]) -> None:
        """Handle raw MQTT messages for diagnostics tracking."""
        # Extract MAC if available
        mac = msg_dict.get('data', {}).get('mac')
        if mac:
            # Store raw message data for pointdata callback
            data._raw_mqtt_message_cache[mac] = {
                'topic': topic,
                'raw_payload': json.dumps(msg_dict),
            }
    
    def on_mqtt_pointdata(mac: str, parsed_pointdata: dict) -> None:
        """Handle MQTT pointdata updates."""
        data.last_mqtt_received = datetime.now(timezone.utc)
        data.mqtt_connected = True
        
        # Store received message for diagnostics
        raw_msg_data = data._raw_mqtt_message_cache.pop(mac, {})
        if raw_msg_data:
            data.recent_mqtt_messages_received.append({
                'timestamp': data.last_mqtt_received,
                'topic': raw_msg_data.get('topic', 'unknown'),
                'raw_payload': raw_msg_data.get('raw_payload', ''),
                'decoded_data': parsed_pointdata,  # Human-readable pointdata
                'mac': mac,
            })
        
        zone_id = data.mac_to_zone_id.get(mac)
        if zone_id and zone_id in data.zone_id_to_entity:
            entity = data.zone_id_to_entity[zone_id]
            # Update zone data from MQTT
            zone = ember.get_zone_by_mac(mac)
            if zone:
                entity._zone = zone
                # Update cached heating state and notify heating sensors (thread-safe)
                try:
                    is_heating = (boiler_state(zone) == EPHBoilerStates.ON)
                    data.zone_heating[zone_id] = is_heating
                    _LOGGER.debug(
                        "MQTT updated zone_heating cache: zone_id=%s, is_heating=%s",
                        zone_id,
                        is_heating,
                    )
                except Exception as err:
                    _LOGGER.debug(
                        "Error updating zone_heating cache for zone_id %s: %s", zone_id, err
                    )
                    # Keep previous state or default to False
                    if zone_id not in data.zone_heating:
                        data.zone_heating[zone_id] = False
                heating_sensor = data.zone_id_to_heating_sensor.get(zone_id)
                if heating_sensor is not None:
                    hass.loop.call_soon_threadsafe(heating_sensor.handle_zone_update, zone)
                if data.system_heating_sensor is not None:
                    hass.loop.call_soon_threadsafe(data.system_heating_sensor.handle_system_update)
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
            
            # Parse log content to extract topic and payload
            # Format: "Topic: {topic}\nPayload: {payload}"
            try:
                lines = content.split('\n')
                topic = None
                payload = None
                mac = None
                
                for line in lines:
                    if line.startswith('Topic: '):
                        topic = line[7:].strip()
                    elif line.startswith('Payload: '):
                        payload = line[9:].strip()
                
                # Try to extract MAC and decode pointData from payload if it's JSON
                decoded_data = {}
                if payload:
                    try:
                        payload_dict = json.loads(payload)
                        mac = payload_dict.get('data', {}).get('mac')
                        # Decode pointData if present
                        pointdata_b64 = payload_dict.get('data', {}).get('pointData')
                        if pointdata_b64:
                            try:
                                decoded_data = decode_point_data(pointdata_b64)
                            except Exception as decode_err:
                                _LOGGER.debug("Error decoding pointData for sent message: %s", decode_err)
                                decoded_data = {}
                    except (json.JSONDecodeError, AttributeError):
                        pass
                
                # Store sent message for diagnostics
                if topic and payload:
                    data.recent_mqtt_messages_sent.append({
                        'timestamp': data.last_mqtt_sent,
                        'topic': topic,
                        'raw_payload': payload,
                        'decoded_data': decoded_data,  # Human-readable pointdata
                        'mac': mac,
                    })
            except Exception as err:
                _LOGGER.debug("Error parsing MQTT log content for diagnostics: %s", err)

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
    # Access messenger directly to set raw message callback
    if hasattr(ember, 'messenger') and ember.messenger:
        ember.messenger.set_on_message_callback(on_mqtt_message)
    
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
