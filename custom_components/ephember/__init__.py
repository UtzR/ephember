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

from .const import CONF_GATEWAY_ID, DOMAIN, EPHBoilerStates

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
        self.device_type: int | None = None
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
    selected_gateway_id = entry.data.get(CONF_GATEWAY_ID)

    try:
        ember = await hass.async_add_executor_job(EphEmber, username, password)
    except RuntimeError as err:
        raise ConfigEntryNotReady(f"Unable to connect to EPH Controls: {err}") from err

    # Get zones to build MAC to zone_id mapping
    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError as err:
        raise ConfigEntryNotReady(f"Unable to get zones from EPH Controls: {err}") from err

    # Filter to selected home if gateway_id is specified
    if selected_gateway_id:
        homes = [home for home in homes if home.get("gatewayid") == selected_gateway_id]
        if not homes:
            raise ConfigEntryNotReady(
                f"Selected home (gateway_id: {selected_gateway_id}) not found"
            )
    elif len(homes) > 1:
        # Multiple homes but no selection - this shouldn't happen with new configs
        # but handle gracefully for existing configs
        _LOGGER.warning(
            "Multiple homes found but no gateway_id selected. Using first home."
        )
        homes = [homes[0]]

    # Create data storage
    data = EphemberData(ember)

    # Extract deviceType and systemType from the selected/filtered home
    if homes:
        selected_home = homes[0]
        # Extract deviceType from the selected home
        device_type = selected_home.get("deviceType")
        _LOGGER.debug("Extracted deviceType from home: %s", device_type)
        if device_type is not None:
            data.device_type = device_type
        
        # Extract systemType from first zone of the selected home
        zones = selected_home.get("zones", [])
        if zones:
            first_zone = zones[0]
            system_type = first_zone.get("systemType")
            _LOGGER.debug("Extracted systemType from zone: %s", system_type)
            if system_type:
                data.system_type = system_type

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

    # Create main device in device registry
    device_registry = dr.async_get(hass)
    
    # Build model string with systemType and deviceType
    model_parts = []
    if data.system_type:
        model_parts.append(data.system_type)
    if data.device_type is not None:
        model_parts.append(f"(type {data.device_type})")
    model = " ".join(model_parts) if model_parts else "Ember System"
    
    # Log for debugging
    _LOGGER.debug(
        "Creating/updating main device: system_type=%s, device_type=%s, model=%s",
        data.system_type,
        data.device_type,
        model,
    )
    
    main_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="EPH Controls Ember",
        manufacturer="EPH Controls",
        model=model,
    )
    
    # Always explicitly update the device model to ensure it's current
    # This handles cases where async_get_or_create doesn't update existing devices
    _LOGGER.debug(
        "Current device model: %s, desired model: %s, device_id: %s",
        main_device.model,
        model,
        main_device.id,
    )
    device_registry.async_update_device(
        main_device.id,
        model=model,
    )
    _LOGGER.debug("Device model update called")

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
            # Update zone data from MQTT - THIS MUST BE CALLED FIRST!
            # This updates the zone's pointDataList with the MQTT data so boiler_state() reads correct values
            updated = ember.update_zone_from_mqtt(mac, parsed_pointdata)
            if updated:
                # Get the updated zone (it's the same dict reference, but ensures it's fresh)
                zone = ember.get_zone_by_mac(mac)
                if zone:
                    # Ensure entity._zone points to the updated zone dict
                    # This is important even if it's the same reference, as it ensures
                    # the entity is using the most up-to-date zone data
                    entity._zone = zone
                    
                    # Force a state refresh by calling async_write_ha_state immediately
                    # This ensures hvac_action property reads the updated boiler_state
                    # This callback runs in MQTT thread, so we need to schedule on event loop
                    # async_write_ha_state() is a @callback method (synchronous but must run on event loop)
                    hass.loop.call_soon_threadsafe(entity.async_write_ha_state)
                    
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
