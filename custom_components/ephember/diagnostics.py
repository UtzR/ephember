"""Support for EPH Controls Ember diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import EphemberConfigEntry
from .const import DOMAIN

TO_REDACT = {"password", "token", "serial"}


def _make_json_serializable(obj: Any, visited: set[int] | None = None) -> Any:
    """Convert object to JSON-serializable format, handling circular references."""
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
        return "<circular reference>"
    visited.add(obj_id)
    
    try:
        # Handle datetime objects
        if hasattr(obj, "isoformat"):
            try:
                return obj.isoformat()
            except Exception:
                return str(obj)
        
        # Handle dict
        if isinstance(obj, dict):
            return {str(k): _make_json_serializable(v, visited) for k, v in obj.items()}
        
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
    # Check if this is the main device
    if (DOMAIN, config_entry.entry_id) not in device.identifiers:
        # Not the main device, return empty diagnostics
        return {}
    
    data = config_entry.runtime_data
    
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
