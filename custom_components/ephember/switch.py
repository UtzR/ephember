"""Support for setpoint modification switch entities of the EPH Controls Ember integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EphemberConfigEntry
from .const import DOMAIN
from .pyephember2.pyephember2 import zone_is_hotwater, zone_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EphemberConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EPH Controls Ember setpoint modification switches from a config entry."""
    data = entry.runtime_data
    ember = data.ember

    try:
        homes = await hass.async_add_executor_job(ember.get_zones)
    except RuntimeError:
        _LOGGER.error("Failed to get zones from EPH Controls")
        return

    # Only create switches for non-Hot Water Controllers (Thermostats, etc.)
    entities = [
        EphemberSetpointSwitch(data, ember, zone, entry)
        for home in homes
        for zone in home["zones"]
        if not zone_is_hotwater(zone)
    ]
    
    # Register switches in data structure for climate entities to access
    for entity in entities:
        data.zone_id_to_switch[entity._zone_id] = entity
    
    async_add_entities(entities)


class EphemberSetpointSwitch(RestoreEntity, SwitchEntity):
    """Representation of a setpoint modification switch for an EPH Controls Ember zone."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermostat"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, data: Any, ember: Any, zone: dict[str, Any], entry: EphemberConfigEntry) -> None:
        """Initialize the setpoint modification switch."""
        self._data = data
        self._ember = ember
        self._entry = entry
        self._zone_name = zone_name(zone)
        self._zone = zone
        self._zone_id = zone["zoneid"]
        self._hot_water = zone_is_hotwater(zone)
        self._attr_unique_id = f"{self._zone_id}_setpoint_enabled"
        
        # Set name to "Device name + Setpoint Control" (e.g., "Hot Water Setpoint Control")
        self._attr_name = f"{self._zone_name} Setpoint Control"
        
        # Default to enabled (True) for thermostats, disabled (False) for Hot Water Controllers
        self._attr_is_on = not self._hot_water

        # Device info - links to the same device as the climate entity
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._zone_id)},
            name=self._zone_name,
            manufacturer="EPH Controls",
        )

    async def async_added_to_hass(self) -> None:
        """Handle entity which will be added to hass."""
        await super().async_added_to_hass()
        
        # Restore the last state (but force False for Hot Water Controllers)
        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state not in ("unknown", "unavailable"):
                if not self._hot_water:
                    # Only restore state for non-hot-water controllers
                    self._attr_is_on = last_state.state == "on"
        
        # Force Hot Water Controllers to always be off
        if self._hot_water:
            self._attr_is_on = False
        
        # Notify climate entity of state change
        self._notify_climate_entity()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on setpoint modification."""
        # Hot Water Controllers cannot have setpoint modification enabled
        if self._hot_water:
            return
        
        self._attr_is_on = True
        self.async_write_ha_state()
        self._notify_climate_entity()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off setpoint modification."""
        self._attr_is_on = False
        self.async_write_ha_state()
        self._notify_climate_entity()

    def _notify_climate_entity(self) -> None:
        """Notify the climate entity that the switch state has changed."""
        if self._zone_id in self._data.zone_id_to_entity:
            climate_entity = self._data.zone_id_to_entity[self._zone_id]
            # Update climate entity state to reflect UI changes
            # async_write_ha_state is a @callback method, safe to call from async context
            climate_entity.async_write_ha_state()
