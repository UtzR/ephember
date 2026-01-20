"""Support for EPH Controls Ember diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import EphemberConfigEntry
from .const import DOMAIN

TO_REDACT = {"password", "token", "serial", "invitecode", "gatewayid"}


def _make_json_serializable(obj: Any, visited: set[int] | None = None) -> Any:
    """Convert object to JSON-serializable format, handling circular references.
    
    Removes Prev/Next fields from schedule data to break circular references.
    Also removes 'days' field as it's a duplicate of 'deviceDays' (created by pyephember2).
    """
    if visited is None:
        visited = set()
    
    # Handle None
    if obj is None:
        return None
    
    # Handle primitive types
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Handle circular references by checking object id
    obj_id = id(obj)
    if obj_id in visited:
        return None  # Return None instead of string for cleaner output
    visited.add(obj_id)
    
    try:
        # Handle datetime objects
        if hasattr(obj, "isoformat"):
            try:
                return obj.isoformat()
            except Exception:
                return str(obj)
        
        # Handle dict - remove Prev/Next/Count fields to break circular refs
        # Also remove 'days' field as it's a duplicate of 'deviceDays' created by pyephember2
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                # Skip Prev, Next, and Count fields that create circular references
                if k in ("Prev", "Next", "Count"):
                    continue
                # Skip 'days' field - it's a duplicate of 'deviceDays' created by pyephember2
                # 'deviceDays' is the original format from the HTTP API
                if k == "days":
                    continue
                result[str(k)] = _make_json_serializable(v, visited)
            return result
        
        # Handle list/tuple
        if isinstance(obj, (list, tuple)):
            return [_make_json_serializable(item, visited) for item in obj]
        
        # Handle Enum
        if hasattr(obj, "value") and hasattr(obj, "name"):
            return obj.value
        
        # Handle other objects - try to convert to dict
        if hasattr(obj, "__dict__"):
            return _make_json_serializable(obj.__dict__, visited)
        
        # Fallback to string representation
        return str(obj)
    finally:
        visited.discard(obj_id)


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: EphemberConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    data = config_entry.runtime_data
    
    # Check if this is the main device
    if (DOMAIN, config_entry.entry_id) in device.identifiers:
        # Main device - return existing diagnostics
        return _get_main_device_diagnostics(data)
    
    # Check if this is a zone device
    zone_id = None
    for identifier in device.identifiers:
        if identifier[0] == DOMAIN and identifier[1] != config_entry.entry_id:
            zone_id = identifier[1]
            break
    
    if zone_id is None:
        # Unknown device type, return empty diagnostics
        return {}
    
    # Zone device - return zone-specific diagnostics
    return _get_zone_device_diagnostics(data, zone_id)


def _get_main_device_diagnostics(data: Any) -> dict[str, Any]:
    """Return diagnostics for the main device."""
    # Build diagnostics data structure with serializable data
    # Convert zones_data to JSON-serializable format first to avoid recursion issues
    zones_data_serialized = None
    if data.last_http_zones_data is not None:
        zones_data_serialized = _make_json_serializable(data.last_http_zones_data)
    
    # Build message lists with serializable data
    recent_received = []
    for msg in list(data.recent_mqtt_messages_received):
        recent_received.append({
            "timestamp": msg.get("timestamp").isoformat() if msg.get("timestamp") else None,
            "topic": msg.get("topic", "unknown"),
            "raw_payload": msg.get("raw_payload", ""),
            "decoded_data": _make_json_serializable(msg.get("decoded_data", {})),
            "mac": msg.get("mac"),
        })
    
    recent_sent = []
    for msg in list(data.recent_mqtt_messages_sent):
        recent_sent.append({
            "timestamp": msg.get("timestamp").isoformat() if msg.get("timestamp") else None,
            "topic": msg.get("topic", "unknown"),
            "raw_payload": msg.get("raw_payload", ""),
            "decoded_data": _make_json_serializable(msg.get("decoded_data", {})),
            "mac": msg.get("mac"),
        })
    
    diagnostics_data: dict[str, Any] = {
        "http_requests": {
            "last_request_timestamp": (
                data.last_http_request.isoformat() if data.last_http_request else None
            ),
            "zones_data": zones_data_serialized,  # Pre-serialized to avoid recursion
        },
        "mqtt": {
            "connected": data.mqtt_connected,
            "last_sent": (
                data.last_mqtt_sent.isoformat() if data.last_mqtt_sent else None
            ),
            "last_received": (
                data.last_mqtt_received.isoformat() if data.last_mqtt_received else None
            ),
            "recent_messages_received": recent_received,
            "recent_messages_sent": recent_sent,
        },
    }
    
    # Redact sensitive data (now safe since all data is already serializable)
    return async_redact_data(diagnostics_data, TO_REDACT)


def _get_zone_device_diagnostics(data: Any, zone_id: str) -> dict[str, Any]:
    """Return diagnostics for a zone device."""
    if not data.last_http_zones_data:
        return {}
    
    # Find the zone matching the zone_id
    zone_data = None
    for home in data.last_http_zones_data:
        for zone in home.get("zones", []):
            if zone.get("zoneid") == zone_id:
                zone_data = zone
                break
        if zone_data:
            break
    
    if not zone_data:
        return {}
    
    # Extract zone-specific fields matching the requested format
    zone_diagnostics: dict[str, Any] = {
        "deviceType": zone_data.get("deviceType"),
        "icon": zone_data.get("icon"),
        "isonline": zone_data.get("isonline"),
        "mac": zone_data.get("mac"),
        "name": zone_data.get("name"),
        "pointDataList": zone_data.get("pointDataList", []),
        "productId": zone_data.get("productId"),
        "systemType": zone_data.get("systemType"),
        "uid": zone_data.get("uid"),
        "zoneid": zone_data.get("zoneid"),
        "timestamp": zone_data.get("timestamp"),
    }
    
    # Make JSON-serializable and redact sensitive data
    zone_diagnostics_serialized = _make_json_serializable(zone_diagnostics)
    return async_redact_data(zone_diagnostics_serialized, TO_REDACT)
