"""
PyEphEmber interface implementation for https://ember.ephcontrols.com/
"""
# pylint: disable=consider-using-f-string

import base64
import datetime
import json
import time
import collections
import threading
import logging

from enum import Enum
from typing import OrderedDict, Callable, Optional, Dict, Any, List

import requests
import paho.mqtt.client as mqtt

# Logger for MQTT operations
_mqtt_logger = logging.getLogger("pyephember2.mqtt")


def decode_point_data(pstr: str) -> Dict[int, Dict[str, Any]]:
    """
    Parse base64-encoded pointData into a dictionary.
    
    Returns dict mapping pointIndex to:
        {
            'index': int,
            'datatype': int,
            'raw_bytes': str (dotted bytes),
            'value': int
        }
    """
    lengths = {1: 1, 2: 2, 4: 2, 5: 4}
    parsed = {}
    mode = "wait"
    datatype = None
    index = None
    value = []

    def bytes_to_int(byte_data):
        result = 0
        for a_byte in byte_data:
            result = result * 256 + int(a_byte)
        return result

    for number in base64.b64decode(pstr):
        if mode == "wait":
            if number != 0:
                continue  # Skip unexpected bytes
            mode = "index"
            continue
        if mode == "index":
            index = number
            mode = "datatype"
            continue
        if mode == "datatype":
            datatype = number
            if datatype not in lengths:
                _mqtt_logger.warning(f"Unknown datatype: {datatype}")
                mode = "wait"
                continue
            mode = "value"
            continue
        if mode == "value":
            value.append(number)
            if len(value) == lengths[datatype]:
                parsed[index] = {
                    'index': index,
                    'datatype': datatype,
                    'raw_bytes': ".".join([str(x) for x in value]),
                    'value': bytes_to_int(value)
                }
                value = []
                mode = "wait"
            continue
    return parsed


class ZoneMode(Enum):
    """
    Modes that a zone can be set too
    """

    AUTO = 0
    ALL_DAY = 1
    ON = 2
    OFF = 3


def GetPointIndex(zone, ephFunction) -> int:
    assert isinstance(ephFunction, EphFunction)
    
    # Extract device type once at the top (needed for multiple cases)
    device_type = zone["deviceType"]
    
    match ephFunction:
        case EphFunction.ADVANCE_ACTIVE:
            return 4
        case EphFunction.CURRENT_TEMP:
            return 5
        case EphFunction.TARGET_TEMP_R:
            match device_type:
                case _:  # deviceType 2, 4, 258, 514, 773
                    return 6
        case EphFunction.TARGET_TEMP_W:
            # Write target temperature - only call zone_mode when we need it (for deviceType 258)
            # This avoids circular dependency since zone_mode needs GetPointIndex for MODE
            match device_type:
                case 258:
                    current_mode = zone_mode(zone)
                    # deviceType 258: AUTO mode uses index 17, ON/MANUAL/OFF uses index 12
                    if current_mode == ZoneMode.AUTO:
                        return 17  # Setpoint (Auto Mode)
                    else:
                        return 12  # Setpoint (Man Mode)
                case 514 | 773:
                    # deviceType 514, 773: Manual mode uses index 12
                    # For AUTO mode, setpoint comes from schedule, but we use 12 for setting
                    return 12  # Manual Mode Setpoint
                case _:  # deviceType 2, 4
                    # These devices use index 6 for all modes
                    return 6
        case EphFunction.MODE:
            match device_type:
                case 258 | 514 | 773:
                    return 11
                case _:
                    return 7
        case EphFunction.BOOST_HOURS:
            match device_type:
                case 258 | 514 | 773:
                    return 13
                case _:
                    return 8
        case EphFunction.BOOST_TIME:
            match device_type:
                case 258 | 514 | 773:
                    return 15
                case _:
                    return 9
        case EphFunction.BOILER_STATE:
            match device_type:
                case 258 | 514:
                    return 18
                case _:
                    return 10
        case EphFunction.BOOST_TEMP:
            return 14
        case EphFunction.MAX_TEMP:
            match device_type:
                case 258 | 514:
                    return 7  # Hi Temp Limit
                case _:
                    return -1  # Not supported
        case EphFunction.MIN_TEMP:
            match device_type:
                case 258 | 514:
                    return 8  # Lo Temp Limit
                case _:
                    return -1  # Not supported
        case _:
            return -1  # No point index found


class EphFunction(Enum):
    """
    EPH function identifiers for pointData operations
    """

    ADVANCE_ACTIVE = 1
    CURRENT_TEMP = 2
    TARGET_TEMP_R = 3
    TARGET_TEMP_W = 4
    MODE = 5
    BOOST_HOURS = 6
    BOOST_TIME = 7
    BOILER_STATE = 8
    BOOST_TEMP = 9
    MAX_TEMP = 10
    MIN_TEMP = 11
    


# """
# Named tuple to hold a command to write data to a zone
# """
ZoneCommand = collections.namedtuple('ZoneCommand', ['name', 'value', 'index'])


def zone_command_to_ints(zone, command):
    """
    Convert a ZoneCommand to an array of integers to send
    """
    type_data = {
        'SMALL_INT': {'id': 1, 'byte_len': 1},
        'TEMP_RO': {'id': 2, 'byte_len': 2},
        'TEMP_RW': {'id': 4, 'byte_len': 2},
        'TIMESTAMP': {'id': 5, 'byte_len': 4}
    }
    writable_command_types = {
        'ADVANCE_ACTIVE': 'SMALL_INT',
        'TARGET_TEMP_W': 'TEMP_RW',
        'MODE': 'SMALL_INT',
        'BOOST_HOURS': 'SMALL_INT',
        'BOOST_TIME': 'TIMESTAMP',
        'BOOST_TEMP': 'TEMP_RW'
    }
    if command.name not in writable_command_types:
        raise ValueError(
            "Cannot write to read-only value "
            "{}".format(command.name)
        )
    
    command_type = writable_command_types[command.name]

    if command.index is not None:
        command_index = command.index
    else:
        command_index = GetPointIndex(zone, EphFunction[command.name])
        if command_index == -1:
            raise ValueError(
                f"No point index found for EphFunction: {command.name}"
            )

    # command header: [0, index, type_id]
    int_array = [0, command_index, type_data[command_type]['id']]

    # now encode and append the value
    send_value = command.value
    if command_type == 'TEMP_RW':
        # The thermostat uses tenths of a degree;
        # send_value is given in degrees, so we convert.
        send_value = int(10*send_value)
    elif command_type == 'TIMESTAMP':
        # send_value can be either an int representing a Unix timestamp,
        # or a datetime. Convert if a datetime.
        if isinstance(command.value, datetime.datetime):
            send_value = int(command.value.timestamp())

    for byte_value in send_value.to_bytes(
            type_data[command_type]['byte_len'], 'big'):
        int_array.append(int(byte_value))

    return int_array


def zone_is_active(zone):
    """
    Check if the zone is on.
    This is a bit of a hack as the new API doesn't have a currently
    active variable
    """
    if zone_is_scheduled_on(zone):
        return True
    # not sure how reliable the next tests are
    return zone_boost_hours(zone) > 0 or zone_advance_active(zone)


def zone_advance_active(zone):
    """
    Check if zone has advance active
    """
    match zone["deviceType"]:
        case 773:
            # Mode not supported
            return False
        case 514:
            # Need to fix, point index or value is not right
            return False
        case 258:
            # Need to fix, point index or value is not right
            return False
        case _: # All other devices (2, 4)
            return zone_pointdata_value(zone, EphFunction.ADVANCE_ACTIVE) != 0


def boiler_state(zone):
    """
    Return the boiler state for a zone, as given by the API
    1 => flame off, 2 => flame on
    """
    return zone_pointdata_value(zone, EphFunction.BOILER_STATE)

def lastKey(dict):
    return list(dict.keys())[-1]


def firstKey(dict):
    return list(dict.keys())[0]


def try_parse_int(value):
    try:
        return int(value), True
    except ValueError:
        return None, False

def scheduletime_to_time(dict, key_name):
    """
    Convert a schedule start/end time (an integer) to a Python time
    For example, x = 173 is converted to 17:30
    """
    if dict.get(key_name) is None:
        return None
    stime = dict[key_name]
    if stime is None:
        return None
    return datetime.time(int(str(stime)[:-1]), 10 * int(str(stime)[-1:]))

def getZoneTime(zone):
    tstamp = time.gmtime(zone["timestamp"] / 1000)
    ts_time = datetime.time(tstamp.tm_hour, tstamp.tm_min)
    ts_wday = tstamp.tm_wday + 1
    if ts_wday == 7:
        ts_wday = 0
    return [ts_time, ts_wday]


def zone_get_running_day(zone):
    todaysDay = zone["days"][getZoneTime(zone)[1]]
    return todaysDay

def zone_get_running_program(zone):
    mode = zone_mode(zone)
    ts_time = getZoneTime(zone)[0]

    todaysDay = zone_get_running_day(zone)
    if todaysDay is None:
        return None

    if mode == ZoneMode.AUTO:
        for key in todaysDay["programs"]:
            program = todaysDay["programs"][key]
            start_time = scheduletime_to_time(program, "startTime")
            end_time = scheduletime_to_time(program, "endTime")
            p_time = scheduletime_to_time(program, "time")
            if (
                start_time is not None
                and end_time is not None
                and start_time <= ts_time <= end_time
            ):
                return program
            elif p_time is not None and p_time >= ts_time:
                # some devices using different programm logic
                # P1 contains only activation time and target temp, need to find currently running program by searching previous programm.
                # Ex: Today is Day 2 9:00am, P1 in that day starts at 10am, current programm is last P from Day 1
                runningProgram = program["Prev"]
                return [runningProgram, program]
        # program not found in that day
        # last program active
        lastProg = todaysDay["programs"][lastKey(todaysDay["programs"])]

        if lastProg.get("time") is None:
            return lastProg
        else:
            return [lastProg, lastProg["Next"]]

    elif mode == ZoneMode.ALL_DAY:
        startProgram = todaysDay["programs"][firstKey(todaysDay["programs"])]
        endProgram = todaysDay["programs"][lastKey(todaysDay["programs"])]
        return [startProgram, endProgram]

    return None

def zone_is_scheduled_on(zone):
    """
    Check if zone is scheduled to be on
    """
    mode = zone_mode(zone)
    if mode == ZoneMode.OFF:
        return False

    if mode == ZoneMode.ON:
        return True

    ts_time = getZoneTime(zone)[0]

    if mode == ZoneMode.AUTO:
        runningPrograms = zone_get_running_program(zone)
        if runningPrograms is None:
            return False
        elif type(runningPrograms) is list:
            # some devices using different programm logic
            # P1 contains only activation time and target temp, need to find currently running program by searching previous programm.
            # Ex: Today is Day 2 9:00am, P1 in that day starts at 10am, current programm is last P from Day 1
            currentTemp = zone_current_temperature(zone)
            targetTemp = runningPrograms[0]["temperature"] / 10

            # Current program found, check if current temp ( minus offset 0.3->0.7 deg after temp was reached) < target temp
            # NB! Some devices like eTrv have settings to adjust turn on/off temperature offcet (not available in Ember app).
            if currentTemp + 0.3 < targetTemp:
                return True
            else:
                return False
        else:
            start_time = scheduletime_to_time(runningPrograms, "startTime")
            end_time = scheduletime_to_time(runningPrograms, "endTime")
            if (
                start_time is not None
                and end_time is not None
                and start_time <= ts_time <= end_time
            ):
                return True

    elif mode == ZoneMode.ALL_DAY:
        runningPrograms = zone_get_running_program(zone)
        first_start_time = scheduletime_to_time(runningPrograms[0], "startTime")
        last_end_time = scheduletime_to_time(runningPrograms[1], "endTime")
        if first_start_time is None or last_end_time is None:
            return False
        if first_start_time <= ts_time <= last_end_time:
            return True

    return False

# Hot water devices - no temperature control
HotWaterDevices = [4]

def zone_is_hotwater(zone):
    if zone["deviceType"] in HotWaterDevices:
        return True
    else:
        return False

def zone_name(zone):
    """
    Get zone name
    """
    return zone["name"]


def zone_is_boost_active(zone):
    """
    Is the boost active for the zone
    """
    return zone_boost_hours(zone) > 0


def zone_boost_hours(zone):
    """
    Return zone boost hours
    """
    return zone_pointdata_value(zone, EphFunction.BOOST_HOURS)


def zone_boost_timestamp(zone):
    """
    Return zone boost hours
    """
    return zone_pointdata_value(zone, EphFunction.BOOST_TIME)


def zone_target_temperature(zone):
    """
    Get target temperature for this zone
    """
    if zone["deviceType"] == 773:
        # in auto mode need to find program target temp.
        if zone_mode(zone) == ZoneMode.AUTO:
            programs = zone_get_running_program(zone)
            if programs is not None:
                return programs[0]["temperature"] / 10
            else:
                return 0
    result = zone_pointdata_value(zone, EphFunction.TARGET_TEMP_R)
    if result is None:
        return 0
    return result / 10

def zone_boost_temperature(zone):
    """
    Get boost temperature for this zone
    """
    result = zone_pointdata_value(zone, EphFunction.BOOST_TEMP)
    if result is None:
        return 0
    return result / 10


def zone_current_temperature(zone):
    """
    Get current temperature for this zone
    """
    result = zone_pointdata_value(zone, EphFunction.CURRENT_TEMP)
    if result is None:
        return 0
    return result / 10


def zone_max_temperature(zone):
    """
    Get maximum temperature for this zone
    """
    result = zone_pointdata_value(zone, EphFunction.MAX_TEMP)
    if result is None:
        # Default values: 60.0 for hot water, 35.0 for others
        return 60.0 if zone_is_hotwater(zone) else 35.0
    return result / 10


def zone_min_temperature(zone):
    """
    Get minimum temperature for this zone
    """
    result = zone_pointdata_value(zone, EphFunction.MIN_TEMP)
    if result is None:
        # Default value: 5.0 for all zones
        return 5.0
    return result / 10


def zone_pointdata_value(zone, ephFunction):
    """
    Get value of given index for this zone, as an integer
    ephFunction should be an EphFunction enum member
    """
    # pylint: disable=unsubscriptable-object
    index = GetPointIndex(zone, ephFunction)
    
    if index == -1:
        return None  # No point index found

    for datum in zone['pointDataList']:
        if datum['pointIndex'] == index:
            return int(datum['value'])

    return None


def zone_mode(zone):
    """
    Get mode for this zone
    Default settings based on next known devices
    deviceTypes 2 | 4:
    AUTO = 0
    ALL_DAY = 1
    ON = 2
    OFF = 3

    deviceTypes 773:
    AUTO = 0
    ON/Manual = 1
    BOOST = 0 ? Could be another point index
    OFF = 4

    deviceTypes 514:
    AUTO = 0
    ADVANCE = 0 ? Could be another point index
    ALL_DAY = 9
    ON/Manual = 10
    BOOST = 0 ? Could be another point index
    OFF = 4
    """

    modeValue = zone_pointdata_value(zone, EphFunction.MODE)
    match modeValue:
        case 0:
            return ZoneMode.AUTO
        case 1:
            match zone["deviceType"]:
                case 2:
                    return ZoneMode.ALL_DAY
                case 4:
                    return ZoneMode.ALL_DAY
                case 258:
                    return ZoneMode.ON
                case 773:
                    return ZoneMode.ON
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 1. "
                        f"Expected deviceType: 2, 4, 258, or 773"
                    )
        case 2:
            match zone["deviceType"]:
                case 2:
                    return ZoneMode.ON
                case 4:
                    return ZoneMode.ON
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 2. "
                        f"Expected deviceType: 2 or 4"
                    )
        case 3:
            match zone["deviceType"]:
                case 2:
                    return ZoneMode.OFF
                case 4:
                    return ZoneMode.OFF
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 3. "
                        f"Expected deviceType: 2 or 4"
                    )
        case 4:
            match zone["deviceType"]:
                case 258:
                    return ZoneMode.OFF
                case 514:
                    return ZoneMode.OFF
                case 773:
                    return ZoneMode.OFF
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 4. "
                        f"Expected deviceType: 258, 514, or 773"
                    )
        case 9:
            match zone["deviceType"]:
                case 514:
                    return ZoneMode.ALL_DAY
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 9. "
                        f"Expected deviceType: 514"
                    )
        case 10:
            match zone["deviceType"]:
                case 514:
                    return ZoneMode.ON
                case _:
                    raise RuntimeError(
                        f"Unhandled deviceType {zone['deviceType']} for modeValue 10. "
                        f"Expected deviceType: 514"
                    )
        case _:
            raise RuntimeError(
                f"Unknown modeValue {modeValue} for zone (deviceType: {zone.get('deviceType', 'unknown')}). "
                f"Expected modeValue: 0, 1, 2, 3, 4, 9, or 10"
            )

def get_zone_mode_value(zone, mode) -> int:
    
    match zone['deviceType']:
        case 773 | 258:
            match mode:
                case ZoneMode.AUTO:
                    return 0
                case ZoneMode.ON:
                    return 1
                case ZoneMode.OFF:
                    return 4
                case _:
                    raise RuntimeError(
                        f"Unhandled ZoneMode {mode} for deviceType {zone['deviceType']}. "
                        f"Expected modes: AUTO, ON, or OFF"
                    )
        case 514:
            match mode:
                case ZoneMode.AUTO:
                    return 0
                case ZoneMode.ALL_DAY:
                    return 9
                case ZoneMode.ON:
                    return 10
                case ZoneMode.OFF:
                    return 4
                case _:
                    raise RuntimeError(
                        f"Unhandled ZoneMode {mode} for deviceType {zone['deviceType']}. "
                        f"Expected modes: AUTO, ALL_DAY, ON, or OFF"
                    )
        case _:
            match mode:
                case ZoneMode.AUTO:
                    return 0
                case ZoneMode.ALL_DAY:
                    return 1
                case ZoneMode.ON:
                    return 2
                case ZoneMode.OFF:
                    return 3
                case _:
                    raise RuntimeError(
                        f"Unhandled ZoneMode {mode} for deviceType {zone['deviceType']}. "
                        f"Expected modes: AUTO, ALL_DAY, ON, or OFF"
                    )


class EphMessenger:
    """
    MQTT interface to the EphEmber API.
    
    Supports both sending commands and subscribing to receive updates.
    """

    def __init__(self, parent):
        self.api_url = 'eu-base-mqtt.topband-cloud.com'
        self.api_port = 18883

        self.client = None
        self.client_id = None
        self.parent = parent
        
        # Subscription state
        self._subscribed = False
        self._loop_running = False
        self._subscribed_topics = []
        
        # Callbacks for received data
        self._on_pointdata_callback: Optional[Callable] = None
        self._on_message_callback: Optional[Callable] = None
        self._on_connect_callback: Optional[Callable] = None
        self._on_disconnect_callback: Optional[Callable] = None
        
        # External log callback for test.py integration
        self._log_callback: Optional[Callable] = None
        
        # Zone state cache (updated from MQTT messages)
        self._zone_state_cache: Dict[str, Dict[int, Any]] = {}

    def _log(self, direction: str, content: str):
        """Log MQTT communication if callback is set."""
        if self._log_callback:
            self._log_callback(direction, content)
        _mqtt_logger.debug(f"[{direction}] {content}")

    def set_log_callback(self, callback: Callable[[str, str], None]):
        """Set callback for logging MQTT communication.
        
        Args:
            callback: Function(direction, content) where direction is 'SEND', 'RECV', 'INFO'
        """
        self._log_callback = callback

    def set_on_pointdata_callback(self, callback: Callable[[str, Dict], None]):
        """Set callback for when pointData is received.
        
        Args:
            callback: Function(zone_mac, parsed_pointdata) called when data arrives
        """
        self._on_pointdata_callback = callback

    def set_on_message_callback(self, callback: Callable[[str, Dict], None]):
        """Set callback for raw MQTT messages.
        
        Args:
            callback: Function(topic, message_dict) called for every message
        """
        self._on_message_callback = callback

    def _zone_command_b64(self, zone, cmd, stop_mqtt=True, timeout=1):
        """
        Send a base64-encoded MQTT command to a zone
        Returns true if the command was published within the timeout
        """
        product_id = zone["productId"]
        uid = zone["uid"]
        topic = "/".join([product_id, uid, "download/pointdata"])

        msg = json.dumps(
            {
                "common": {
                    "serial": 7870,
                    "productId": product_id,
                    "uid": uid,
                    "timestamp": str(int(1000*time.time()))
                },
                "data": {
                    "mac": zone['mac'],
                    "pointData": cmd
                }
            }
        )

        # Log the outgoing message
        self._log('SEND', f"Topic: {topic}\nPayload: {msg}")

        started_locally = False
        if not self.client or not self.client.is_connected():
            started_locally = True
            self.start()

        pub = self.client.publish(topic, msg, 0)
        pub.wait_for_publish(timeout=timeout)

        if started_locally and stop_mqtt and not self._subscribed:
            self.stop()

        return pub.is_published()

    def _internal_on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Internal callback for MQTT connection."""
        self._log('INFO', f"Connected to MQTT broker (reason: {reason_code})")
        
        # Subscribe to topics
        for topic in self._subscribed_topics:
            client.subscribe(topic, 0)
            self._log('INFO', f"Subscribed to: {topic}")
        
        if self._on_connect_callback:
            self._on_connect_callback(client, userdata, flags, reason_code)

    def _internal_on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        """Internal callback for MQTT disconnection."""
        self._log('INFO', f"Disconnected from MQTT broker (reason: {reason_code})")
        self._subscribed = False
        
        if self._on_disconnect_callback:
            self._on_disconnect_callback(client, userdata, disconnect_flags, reason_code)

    def _internal_on_message(self, client, userdata, message):
        """Internal callback for received MQTT messages."""
        try:
            topic = message.topic
            payload = message.payload.decode("utf-8").rstrip('\0')
            
            self._log('RECV', f"Topic: {topic}\nPayload: {payload}")
            
            msg_dict = json.loads(payload)
            
            # Call raw message callback if set
            if self._on_message_callback:
                self._on_message_callback(topic, msg_dict)
            
            # Parse pointData if present
            if 'data' in msg_dict and 'pointData' in msg_dict['data']:
                mac = msg_dict.get('data', {}).get('mac', 'unknown')
                pointdata_b64 = msg_dict['data']['pointData']
                parsed = decode_point_data(pointdata_b64)
                
                # Update local cache
                if mac not in self._zone_state_cache:
                    self._zone_state_cache[mac] = {}
                self._zone_state_cache[mac].update({k: v['value'] for k, v in parsed.items()})
                
                # Update the parent's cached zone data (HTTP cache)
                # This allows legacy functions to see MQTT updates
                if self.parent:
                    self.parent.update_zone_from_mqtt(mac, parsed)
                
                # Call pointdata callback if set
                if self._on_pointdata_callback:
                    self._on_pointdata_callback(mac, parsed)
                    
        except Exception as e:
            self._log('INFO', f"Error processing message: {e}")
            _mqtt_logger.exception("Error processing MQTT message")

    # Public interface

    def start(self, callbacks=None, loop_start=False):
        """
        Start MQTT client.
        
        Args:
            callbacks: Optional dict of callback names to functions (legacy support)
            loop_start: If True, start the network loop in a background thread
        
        Returns:
            The mqtt.Client instance
        """
        credentials = self.parent.messenging_credentials()
        self.client_id = '{}_{}'.format(
            credentials['user_id'], str(int(1000*time.time()))
        )
        token = credentials['token']

        mclient = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, self.client_id)
        mclient.tls_set()
        self.client = mclient

        user_name = "app/{}".format(token)
        mclient.username_pw_set(user_name, token)

        # Set internal callbacks
        mclient.on_connect = self._internal_on_connect
        mclient.on_disconnect = self._internal_on_disconnect
        mclient.on_message = self._internal_on_message

        # Legacy callback support
        if callbacks is not None:
            for key in callbacks.keys():
                if key == 'on_connect':
                    self._on_connect_callback = callbacks[key]
                elif key == 'on_message':
                    self._on_message_callback = callbacks[key]
                else:
                    setattr(mclient, key, callbacks[key])

        self._log('INFO', f"Connecting to {self.api_url}:{self.api_port}")
        mclient.connect(self.api_url, self.api_port)

        if loop_start:
            mclient.loop_start()
            self._loop_running = True

        return mclient

    def stop(self):
        """
        Disconnect MQTT client if connected.
        
        Returns:
            True if disconnected, False if no client
        """
        if not self.client:
            return False
        
        if self._loop_running:
            self.client.loop_stop()
            self._loop_running = False
            
        if self.client.is_connected():
            self.client.disconnect()
            
        self._subscribed = False
        self._log('INFO', "MQTT client stopped")
        return True

    def subscribe_to_zone(self, zone) -> bool:
        """
        Subscribe to updates for a specific zone.
        
        Args:
            zone: Zone dict containing productId and uid
            
        Returns:
            True if subscription initiated
        """
        product_id = zone["productId"]
        uid = zone["uid"]
        topic = "/".join([product_id, uid, "upload/pointdata"])
        
        if topic not in self._subscribed_topics:
            self._subscribed_topics.append(topic)
        
        if self.client and self.client.is_connected():
            self.client.subscribe(topic, 0)
            self._log('INFO', f"Subscribed to: {topic}")
            self._subscribed = True
            return True
        return False

    def subscribe_to_all_zones(self, zones: List[Dict]) -> int:
        """
        Subscribe to updates for multiple zones.
        
        Args:
            zones: List of zone dicts
            
        Returns:
            Number of zones subscribed to
        """
        count = 0
        for zone in zones:
            if self.subscribe_to_zone(zone):
                count += 1
        return count

    def start_listening(self, zones: List[Dict] = None):
        """
        Start MQTT client, subscribe to zones, and begin listening loop.
        
        Args:
            zones: Optional list of zones to subscribe to
        """
        # Add topics to subscribe list
        if zones:
            for zone in zones:
                product_id = zone["productId"]
                uid = zone["uid"]
                topic = "/".join([product_id, uid, "upload/pointdata"])
                if topic not in self._subscribed_topics:
                    self._subscribed_topics.append(topic)
        
        # Start with background loop
        self.start(loop_start=True)
        self._subscribed = True

    def get_cached_zone_state(self, mac: str) -> Optional[Dict[int, Any]]:
        """
        Get cached state for a zone (updated from MQTT messages).
        
        Args:
            mac: Zone MAC address
            
        Returns:
            Dict mapping EphFunction to value, or None if not cached
        """
        return self._zone_state_cache.get(mac)

    def send_zone_commands(self, zone, commands, stop_mqtt=True, timeout=1):
        """
        Bundles the given array of ZoneCommand objects
        to a single MQTT command and sends to the named zone.

        If a single ZoneCommand is given, send just that.

        Returns true if the bundled command was published within the timeout.

        For example, to set target temperature to 19:

          send_zone_command("Zone_name", ZoneCommand('TARGET_TEMP_W', 19))

        """
        def ints_to_b64_cmd(int_array):
            """
            Convert an array of integers to a byte array and
            return its base64 string in ascii
            """
            return base64.b64encode(bytes(int_array)).decode("ascii")

        if isinstance(commands, ZoneCommand):
            commands = [commands]

        ints_cmd = [x for cmd in commands for x in zone_command_to_ints(zone, cmd)]

        # Don't stop MQTT if we're subscribed
        effective_stop = stop_mqtt and not self._subscribed

        return self._zone_command_b64(
            zone, ints_to_b64_cmd(ints_cmd), effective_stop, timeout
        )


class EphEmber:
    """
    Interacts with a EphEmber thermostat via API.
    Example usage: t = EphEmber('me@somewhere.com', 'mypasswd')
                   t.get_zone_temperature('myzone') # Get temperature
    """

    # pylint: disable=too-many-public-methods

    def _http(self, endpoint, *, method=requests.post, headers=None,
              send_token=False, data=None, timeout=10):
        """
        Send a request to the http API endpoint
        method should be requests.get or requests.post
        """
        if not headers:
            headers = {}

        if send_token:
            if not self._do_auth():
                raise RuntimeError("Unable to login")
            headers["Authorization"] = self._login_data["data"]["token"]

        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"

        url = "{}{}".format(self.http_api_base, endpoint)

        if data and isinstance(data, dict):
            data = json.dumps(data)

        response = method(url, data=data, headers=headers, timeout=timeout)

        if response.status_code != 200:
            raise RuntimeError(
                "{} response code".format(response.status_code)
            )

        return response

    def _requires_refresh_token(self):
        """
        Check if a refresh of the token is needed
        """
        expires_on = self._login_data["last_refresh"] + \
            datetime.timedelta(seconds=self._refresh_token_validity_seconds)
        refresh = datetime.datetime.utcnow() + datetime.timedelta(seconds=30)
        return expires_on < refresh

    def _request_token(self, force=False):
        """
        Request a new auth token
        """
        if self._login_data is None:
            raise RuntimeError("Don't have a token to refresh")

        if not force:
            if not self._requires_refresh_token():
                # no need to refresh as token is valid
                return True

        response = self._http(
            "appLogin/refreshAccessToken",
            method=requests.get,
            headers={'Authorization':
                     self._login_data['data']['refresh_token']}
        )

        refresh_data = response.json()

        if 'token' not in refresh_data.get('data', {}):
            return False

        self._login_data['data'] = refresh_data['data']
        self._login_data['last_refresh'] = datetime.datetime.utcnow()

        return True

    def _login(self):
        """
        Login using username / password and get the first auth token
        """
        self._login_data = None

        response = self._http(
            "appLogin/login",
            data={
                'userName': self._user['username'],
                'password': self._user['password']
            }
        )

        self._login_data = response.json()
        if self._login_data['status'] != 0:
            self._login_data = None
            return False
        self._login_data["last_refresh"] = datetime.datetime.utcnow()

        if ('data' in self._login_data
                and 'token' in self._login_data['data']):
            return True

        self._login_data = None
        return False

    def _do_auth(self):
        """
        Do authentication to the system (if required)
        """
        if self._login_data is None:
            return self._login()

        return self._request_token()

    def _get_user_details(self):
        """
        Get user details [user/selectUser]
        """
        response = self._http(
            "user/selectUser", method=requests.get,
            send_token=True
        )
        user_details = response.json()
        if user_details['status'] != 0:
            return {}
        return user_details

    def _get_user_id(self, force=False):
        """
        Get user ID
        """
        if not force and self._user['user_id']:
            return self._user['user_id']

        user_details = self._get_user_details()
        data = user_details.get('data', {})
        if 'id' not in data:
            raise RuntimeError("Cannot get user ID")
        self._user['user_id'] = str(data['id'])
        return self._user['user_id']

    def _get_first_gateway_id(self):
        """
        Get the first gatewayid associated with the account
        """
        if not self._homes:
            raise RuntimeError("Cannot get gateway id from list of homes.")
        return self._homes[0]['gatewayid']

    def _set_zone_target_temperature(self, zone, target_temperature):
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('TARGET_TEMP_W', target_temperature, None)
        )

    def _set_zone_boost_temperature(self, zone, target_temperature):
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('BOOST_TEMP', target_temperature, None)
        )

    def _set_zone_advance(self, zone, advance=True):
        if advance:
            advance = 1
        else:
            advance = 0
        return self.messenger.send_zone_commands(
            zone,
            ZoneCommand('ADVANCE_ACTIVE', advance, None)
        )

    def _set_zone_boost(self, zone, boost_temperature, num_hours, timestamp=0):
        """
        Internal method to set zone boost

        num_hours validation:
        - For device 258, 514, 773: max 1 hour (clamped if > 1)
        - For device 2, 4: max 3 hours (clamped if > 3)

        If boost_temperature is not None, send that

        Timestamp calculation:
        - For device 258, 514, 773: current timestamp + num_hours (if timestamp=0)
        - For device 2, 4: current timestamp only (if timestamp=0)

        If timestamp is None, do not send timestamp at all.
        (maybe results in permanent boost?)
        """
        device_type = zone["deviceType"]
        
        # Validate and clamp num_hours based on device type
        if device_type in (258, 514, 773):
            if num_hours > 1:
                num_hours = 1
        else:
            if num_hours > 3:
                num_hours = 3
        
        cmds = [ZoneCommand('BOOST_HOURS', num_hours, None)]
        if boost_temperature is not None:
            cmds.append(ZoneCommand('BOOST_TEMP', boost_temperature, None))
        if timestamp is not None:
            if timestamp == 0:
                if device_type in (258, 514, 773):
                    # For device 258, 514, 773: current time + num_hours
                    timestamp = int((datetime.datetime.now() + datetime.timedelta(hours=num_hours)).timestamp())
                else:
                    # For device 2, 4: current time only
                    timestamp = int(datetime.datetime.now().timestamp())
            cmds.append(ZoneCommand('BOOST_TIME', timestamp, None))
        return self.messenger.send_zone_commands(zone, cmds)

    def _set_zone_mode(self, zone, mode):
        """
        Internal method to set zone mode.
        
        Args:
            zone: Zone dict
            mode: ZoneMode enum value
        """
        assert isinstance(mode, ZoneMode)
        modevalue = get_zone_mode_value(zone, mode)
        return self.messenger.send_zone_commands(
            zone, ZoneCommand('MODE', modevalue, None)
        )

    # Public interface

    def messenging_credentials(self):
        """
        Credentials required by EphMessenger
        """
        if not self._do_auth():
            raise RuntimeError("Unable to login")

        return {
            'user_id': self._get_user_id(),
            'token': self._login_data["data"]["token"]
        }

    def list_homes(self):
        """
        List the homes available for this user
        """
        response = self._http(
            "homes/list", method=requests.get, send_token=True
        )
        homes = response.json()
        status = homes.get('status', 1)
        if status != 0:
            raise RuntimeError("Error getting home: {}".format(status))

        return homes.get("data", [])

    def get_home_details(self, gateway_id=None, force=False):
        """
        Get the details about a home (API call: homes/detail)
        If no gateway_id is passed, the first gateway found is used.
        """
        if self._home_details and not force:
            return self._home_details

        if gateway_id is None:
            if not self._homes:
                self._homes = self.list_homes()
            gateway_id = self._get_first_gateway_id()

        response = self._http(
            "homes/detail", send_token=True,
            data={"gateWayId": gateway_id}
        )

        home_details = response.json()

        status = home_details.get('status', 1)
        if status != 0:
            raise RuntimeError(
                "Error getting details from home: {}".format(status))

        if "data" not in home_details or "homes" not in home_details["data"]:
            raise RuntimeError(
                "Error getting details from home: no home data found")

        self._home_details = home_details['data']

        return home_details["data"]

    def lastKey(dict):
        return list(dict.keys())[-1]

    def firstKey(dict):
        return list(dict.keys())[0]
    
    # ["homes"]
    def get_homes(self):
        """
        Get the data about a home (API call: homesVT/zoneProgram).
        """

        if (
            self.NextHomeUpdateDaytime is None
            or datetime.datetime.now() > self.NextHomeUpdateDaytime
        ):
            self._homes = self.list_homes()
        else:
            return self._homes

        for home in self._homes:
            home["zones"] = []
            gateway_id = home["gatewayid"]

            response = self._http(
                "homesVT/zoneProgram", send_token=True, data={"gateWayId": gateway_id}
            )

            homezones = response.json()

            status = homezones.get("status", 1)
            if status != 0:
                raise RuntimeError("Error getting zones from home: {}".format(status))

            if "data" not in homezones:
                raise RuntimeError("Error getting zones from home: no data found")
            if "timestamp" not in homezones:
                raise RuntimeError("Error getting zones from home: no timestamp found")

            for zone in homezones["data"]:
                # build programs
                zone["days"] = {}
                prevProgramm = None
                for day in sorted(
                    zone["deviceDays"], key=lambda x: x["dayType"], reverse=False
                ):
                    day["programs"] = {}
                    keys = day.keys()
                    for key in keys:
                        if key.startswith("p"):
                            tryGetId = try_parse_int(key[1:])
                            if tryGetId[1]:
                                programm = day[key]
                                if programm is not None:
                                    if prevProgramm is not None:
                                        programm["Prev"] = prevProgramm
                                    programm["Count"] = tryGetId[0]
                                    prevProgramm = programm
                                    day["programs"][tryGetId[0]] = programm
                    zone["days"][day["dayType"]] = day
                # reverse loop to connect all Prev programs
                lastProgramm = None
                firstProgramm = None
                for day in OrderedDict(sorted(zone["days"].items(), reverse=True)):
                    if lastProgramm is not None:
                        firstProgramm = zone["days"][day]["programs"][
                            lastKey(zone["days"][day]["programs"])
                        ]
                        lastProgramm["Prev"] = firstProgramm
                    lastProgramm = zone["days"][day]["programs"][
                        firstKey(zone["days"][day]["programs"])
                    ]

                lastProgramm["Prev"] = firstProgramm

                firstDayPrograms = zone["days"][firstKey(zone["days"])]["programs"]
                firstProgram = firstDayPrograms[firstKey(firstDayPrograms)]
                nextProgram = firstProgram
                for day in OrderedDict(sorted(zone["days"].items(), reverse=True)):
                    orderedProgs = OrderedDict(
                        sorted(zone["days"][day]["programs"].items(), reverse=True)
                    )
                    for progNum in orderedProgs:
                        program = zone["days"][day]["programs"][progNum]
                        program["Next"] = nextProgram
                        nextProgram = program

                zone["timestamp"] = homezones["timestamp"]
                home["zones"].append(zone)

        self.NextHomeUpdateDaytime = datetime.datetime.now() + datetime.timedelta(
            seconds=10
        )
        return self._homes

    def get_zones(self):
        """
        Get all zones
        """
        home_data = self.get_homes()
        if not home_data:
            return []

        return home_data

    def get_zone_names(self):
        """
        Get the name of all zones
        """
        zone_names = []
        for zone in self.get_zones():
            zone_names.append(zone['name'])

        return zone_names

    def get_zone(self, zoneid):
        """
        Get the information about a particular zone
        """
        for home in self.get_zones():
            for zone in home['zones']:
                if zoneid == zone['zoneid']:
                    return zone

        raise RuntimeError("Unknown zone: %s" % zoneid)

    def is_zone_active(self, zoneid):
        """
        Check if a zone is active
        """
        zone = self.get_zone(zoneid)
        return zone_is_active(zone)

    def is_zone_boiler_on(self, zoneid):
        """
        Check if the named zone's boiler is on and burning fuel (experimental)
        """
        zone = self.get_zone(zoneid)
        return boiler_state(zone) == 2

    def get_zone_temperature(self, zoneid):
        """
        Get the temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_current_temperature(zone)

    def get_zone_max_temperature(self, zoneid):
        """
        Get the maximum temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_max_temperature(zone)

    def get_zone_min_temperature(self, zoneid):
        """
        Get the minimum temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_min_temperature(zone)

    def get_zone_target_temperature(self, zoneid):
        """
        Get the temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_target_temperature(zone)

    def get_zone_boost_temperature(self, zoneid):
        """
        Get the boost target temperature for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_boost_temperature(zone)

    def is_boost_active(self, zoneid):
        """
        Check if boost is active for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_is_boost_active(zone)

    def boost_hours(self, zoneid):
        """
        Get the boost duration for a zone, in hours
        """
        zone = self.get_zone(zoneid)
        return zone_boost_hours(zone)

    def boost_timestamp(self, zoneid):
        """
        Get the timestamp recorded for the boost
        """
        zone = self.get_zone(zoneid)
        return datetime.datetime.fromtimestamp(zone_boost_timestamp(zone))

    def is_target_temperature_reached(self, zoneid):
        """
        Check if a zone temperature has reached the target temperature
        """
        zone = self.get_zone(zoneid)
        return zone_current_temperature(zone) >= zone_target_temperature(zone)

    def set_zone_target_temperature(self, zoneid, target_temperature):
        """
        Set the target temperature for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_target_temperature(
            zone, target_temperature
        )

    def set_zone_boost_temperature(self, zoneid, target_temperature):
        """
        Set the boost target temperature for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_boost_temperature(
            zone, target_temperature
        )

    def set_zone_advance(self, zoneid, advance_state=True):
        """
        Set the advance state for a named zone
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_advance(
            zone, advance_state
        )

    def activate_zone_boost(self, zoneid, boost_temperature=None,
                            num_hours=1, timestamp=0):
        """
        Turn on boost for a named zone

        If boost_temperature is not None, send that

        If timestamp is 0 (or omitted), use current timestamp

        If timestamp is None, do not send timestamp at all.
        (maybe results in permanent boost?)

        """
        return self._set_zone_boost(
            self.get_zone(zoneid), boost_temperature,
            num_hours, timestamp=timestamp
        )

    def deactivate_zone_boost(self, zone):
        """
        Turn off boost for a named zone
        """
        return self.activate_zone_boost(zone, num_hours=0, timestamp=None)

    def set_zone_mode(self, zoneid, mode):
        """
        Set the mode by using the name of the zone
        Supported zones are available in the enum ZoneMode
        """

        assert isinstance(mode, ZoneMode)

        zone = self.get_zone(zoneid)
        return self._set_zone_mode(zone, mode)

    def get_zone_mode(self, zoneid):
        """
        Get the mode for a zone
        """
        zone = self.get_zone(zoneid)
        return zone_mode(zone)

    # =========================================================================
    # MQTT-specific methods
    # =========================================================================
    
    def set_mqtt_log_callback(self, callback: Callable[[str, str], None]):
        """Set callback for MQTT communication logging.
        
        Args:
            callback: Function(direction, content) for logging
        """
        self.messenger.set_log_callback(callback)

    def set_mqtt_pointdata_callback(self, callback: Callable[[str, Dict], None]):
        """Set callback for when MQTT pointData is received.
        
        Args:
            callback: Function(zone_mac, parsed_pointdata)
        """
        self.messenger.set_on_pointdata_callback(callback)

    def start_mqtt_listener(self, zones: List[Dict] = None):
        """Start MQTT listener to receive zone updates.
        
        Args:
            zones: Optional list of zone dicts to subscribe to.
                   If None, call get_zones() first and subscribe to all.
        """
        if zones is None:
            homes = self.get_zones()
            zones = []
            for home in homes:
                zones.extend(home.get('zones', []))
        
        self.messenger.start_listening(zones)

    def stop_mqtt_listener(self):
        """Stop MQTT listener."""
        self.messenger.stop()

    def is_mqtt_connected(self) -> bool:
        """Check if MQTT client is connected."""
        return self.messenger.client and self.messenger.client.is_connected()

    def get_mqtt_cached_state(self, mac: str) -> Optional[Dict[int, Any]]:
        """Get cached zone state from MQTT updates.
        
        Args:
            mac: Zone MAC address
            
        Returns:
            Dict mapping EphFunction to value, or None
        """
        return self.messenger.get_cached_zone_state(mac)

    # MQTT control methods - these use MQTT directly (existing behavior)
    # The existing set_* methods already use MQTT via messenger.send_zone_commands()
    # These explicit _mqtt variants make it clear which transport is used
    
    def set_zone_target_temperature_mqtt(self, zoneid, target_temperature) -> bool:
        """Set target temperature via MQTT.
        
        Args:
            zoneid: Zone ID
            target_temperature: Target temperature in degrees
            
        Returns:
            True if command was published successfully
        """
        zone = self.get_zone(zoneid)
        return self._set_zone_target_temperature(zone, target_temperature)

    def set_zone_mode_mqtt(self, zoneid, mode: ZoneMode) -> bool:
        """Set zone mode via MQTT.
        
        Args:
            zoneid: Zone ID
            mode: ZoneMode enum value
            
        Returns:
            True if command was published successfully
        """
        assert isinstance(mode, ZoneMode)
        zone = self.get_zone(zoneid)
        return self._set_zone_mode(zone, mode)

    def activate_zone_boost_mqtt(self, zoneid, boost_temperature=None,
                                  num_hours=1, timestamp=0) -> bool:
        """Activate boost via MQTT.
        
        Args:
            zoneid: Zone ID
            boost_temperature: Optional boost temperature
            num_hours: Boost duration (1, 2, or 3)
            timestamp: Boost start timestamp (0 for now, None to omit)
            
        Returns:
            True if command was published successfully
        """
        return self._set_zone_boost(
            self.get_zone(zoneid), boost_temperature,
            num_hours, timestamp=timestamp
        )

    def deactivate_zone_boost_mqtt(self, zoneid) -> bool:
        """Deactivate boost via MQTT.
        
        Args:
            zoneid: Zone ID
            
        Returns:
            True if command was published successfully
        """
        return self.activate_zone_boost_mqtt(zoneid, num_hours=0, timestamp=None)

    def turn_zone_on_mqtt(self, zoneid) -> bool:
        """Turn zone ON via MQTT.
        
        Args:
            zoneid: Zone ID
            
        Returns:
            True if command was published successfully
        """
        return self.set_zone_mode_mqtt(zoneid, ZoneMode.ON)

    def turn_zone_off_mqtt(self, zoneid) -> bool:
        """Turn zone OFF via MQTT.
        
        Args:
            zoneid: Zone ID
            
        Returns:
            True if command was published successfully
        """
        return self.set_zone_mode_mqtt(zoneid, ZoneMode.OFF)

    def reset_login(self):
        """
        reset the login data to force a re-login
        """
        self._login_data = None

    def update_zone_from_mqtt(self, mac: str, parsed_pointdata: Dict[int, Dict]) -> bool:
        """
        Update cached zone data from MQTT pointData.
        
        This allows MQTT updates to be reflected in the cached HTTP data,
        so legacy functions like zone_mode(), zone_current_temperature() etc.
        will return the most up-to-date values without requiring an HTTP refresh.
        
        Args:
            mac: Zone MAC address
            parsed_pointdata: Dict from decode_point_data(), mapping pointIndex to
                              {'index': int, 'datatype': int, 'value': int}
        
        Returns:
            True if zone was found and updated, False otherwise
        """
        if not self._homes:
            _mqtt_logger.debug("update_zone_from_mqtt: No homes cached")
            return False
        
        # Find zone by MAC
        for home in self._homes:
            for zone in home.get('zones', []):
                if zone.get('mac') == mac:
                    zone_name_str = zone.get('name', 'Unknown')
                    _mqtt_logger.debug(f"Found zone '{zone_name_str}' for MAC {mac}")
                    
                    # Ensure pointDataList exists
                    if 'pointDataList' not in zone:
                        zone['pointDataList'] = []
                    
                    # Update each point from MQTT data
                    for point_index, point_info in parsed_pointdata.items():
                        new_value = str(point_info['value'])
                        
                        # Find existing point by index
                        point_found = False
                        for i, point in enumerate(zone['pointDataList']):
                            # Handle both int and string pointIndex
                            existing_index = point.get('pointIndex')
                            if existing_index == point_index or str(existing_index) == str(point_index):
                                # Update in place
                                zone['pointDataList'][i]['value'] = new_value
                                point_found = True
                                _mqtt_logger.debug(f"  Updated EphFunction {point_index} = {new_value}")
                                break
                        
                        if not point_found:
                            # Add new point
                            zone['pointDataList'].append({
                                'pointIndex': point_index,
                                'value': new_value
                            })
                            _mqtt_logger.debug(f"  Added EphFunction {point_index} = {new_value}")
                    
                    # Update timestamp
                    zone['timestamp'] = int(time.time() * 1000)
                    zone['_last_mqtt_update'] = datetime.datetime.now().isoformat()
                    
                    _mqtt_logger.info(f"Updated zone '{zone_name_str}' from MQTT with {len(parsed_pointdata)} points")
                    return True
        
        _mqtt_logger.debug(f"update_zone_from_mqtt: Zone with MAC {mac} not found")
        return False

    def get_zone_by_mac(self, mac: str):
        """
        Get zone information by MAC address.
        
        Args:
            mac: Zone MAC address
            
        Returns:
            Zone dict or None if not found
        """
        if not self._homes:
            return None
        
        for home in self._homes:
            for zone in home.get('zones', []):
                if zone.get('mac') == mac:
                    return zone
        return None

    # Ctor
    def __init__(self, username, password, cache_home=False):
        """Performs login and save session cookie."""

        if cache_home:
            raise RuntimeError("cache_home not implemented")

        self._login_data = None
        self._user = {
            'user_id': None,
            'username': username,
            'password': password
        }

        # This is the list of homes / gateways associated with the account.
        self._homes = None

        self._home_details = None

        self.NextHomeUpdateDaytime = None

        self._refresh_token_validity_seconds = 1800

        self.http_api_base = 'https://eu-https.topband-cloud.com/ember-back/'

        self.messenger = EphMessenger(self)

        if not self._login():
            raise RuntimeError("Unable to login.")