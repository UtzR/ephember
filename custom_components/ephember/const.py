"""Constants for the EPH Controls Ember integration."""

from enum import IntEnum

DOMAIN = "ephember"
CONF_SCAN_INTERVAL = "scan_interval"


class EPHBoilerStates(IntEnum):
    """Boiler state helper."""

    OFF = 1
    ON = 2


