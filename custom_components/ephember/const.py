"""Constants for the EPH Controls Ember integration."""

from enum import IntEnum

DOMAIN = "ephember"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_GAS_CONSUMPTION_RATE = "gas_consumption_rate"


class EPHBoilerStates(IntEnum):
    """Boiler state helper."""

    OFF = 1
    ON = 2


