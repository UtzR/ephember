"""Support for EPH Controls Ember diagnostics."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import EphemberConfigEntry
from .const import DOMAIN

TO_REDACT = {"password", "token", "serial"}


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: EphemberConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    # Check if this is the main device
    if (DOMAIN, config_entry.entry_id) not in device.identifiers:
        # Not the main device, return empty diagnostics
        return {}
    
    data = config_entry.runtime_data
    
    # Build diagnostics data structure
    diagnostics_data: dict[str, Any] = {
        "http_requests": {
            "last_request_timestamp": (
                data.last_http_request.isoformat() if data.last_http_request else None
            ),
            "zones_data": data.last_http_zones_data,  # All zones from last HTTP request
        },
        "mqtt": {
            "connected": data.mqtt_connected,
            "last_sent": (
                data.last_mqtt_sent.isoformat() if data.last_mqtt_sent else None
            ),
            "last_received": (
                data.last_mqtt_received.isoformat() if data.last_mqtt_received else None
            ),
            "recent_messages_received": [
                {
                    "timestamp": msg["timestamp"].isoformat(),
                    "topic": msg["topic"],
                    "raw_payload": msg["raw_payload"],
                    "decoded_data": msg["decoded_data"],  # Human-readable pointdata
                    "mac": msg.get("mac"),
                }
                for msg in list(data.recent_mqtt_messages_received)
            ],
            "recent_messages_sent": [
                {
                    "timestamp": msg["timestamp"].isoformat(),
                    "topic": msg["topic"],
                    "raw_payload": msg["raw_payload"],
                    "mac": msg.get("mac"),
                }
                for msg in list(data.recent_mqtt_messages_sent)
            ],
        },
    }
    
    # Redact sensitive data
    return async_redact_data(diagnostics_data, TO_REDACT)
