# EPH Controls Ember - Enhanced Home Assistant Integration

A custom Home Assistant integration for EPH Controls Ember heating systems with enhanced functionality beyond the core Home Assistant integration.

## Features

This integration provides the following enhancements over the [original Home Assistant ephember integration](https://www.home-assistant.io/integrations/ephember/):

### ✅ Support of Additional EPH Devices and Features 
- Device families such as EMBER-PS, EBER-TS2, EMBER-PS2, EMBER-RS  
- Features such as Boost, Auto schedule, and All Day mode 

### ✅ UI Configuration
- Configure the integration via Home Assistant UI (Settings → Devices & Services → Add Integration)
- No need to edit `configuration.yaml`
- Validates credentials during setup

### ✅ Additional Sensors
- Sensors to monitor heating on times, heating duration, gas consumption 

### ✅ Improved State Updates and MQTT Support
- State refresh after changing preset, mode, or temperature
- Clears API cache to ensure fresh data
- Uses MQTT for improved state synchronization


## Installation

### HACS (Recommended)
1. Add this repository as a custom repository in HACS
2. Install "EPH Controls Ember"
3. Restart Home Assistant
4. Add the integration via Settings → Devices & Services

### Manual Installation
1. Copy the `ephember` folder to your `custom_components` directory
2. Restart Home Assistant
3. Add the integration via Settings → Devices & Services

## Configuration

### Via UI (Recommended)
1. Go to **Settings → Devices & Services**
2. Click **Add Integration**
3. Search for "EPH Controls Ember"
4. Enter your EPH Controls account credentials
5. **Home Selection** (if multiple homes are detected):
   - If your account has multiple homes/gateways, you will be presented with a selection screen
   - Choose which home you want to integrate
   - Each home is displayed with its name, system type (e.g., EMBER-PS, EMBER-TS2), and device type
   - Only zones from the selected home will be used to create devices and entities
   - If you have only one home, this step is skipped automatically
6. Configure optional parameters:
   - **Scan Interval**: Time between HTTP API polling requests (default: 300 seconds, range: 60-3600 seconds)
   - **Gas Consumption Rate**: Gas consumption rate of your boiler in m³ per hour (default: 1.5 m³/h, range: 0.1-10.0 m³/h)

You can also modify these settings later by going to the integration's options in **Settings → Devices & Services → EPH Controls Ember → Options**.

### Configuration Parameters

#### Home Selection
- **When Required**: Only shown if your EPH Controls account has multiple homes/gateways
- **Description**: During setup, if multiple homes are detected, you must select which home to integrate. The selection screen shows each home with:
  - Home name
  - System type (e.g., EMBER-PS, EMBER-TS2, EMBER-PS2)
  - Device type (e.g., type 1, type 3)
- **Behavior**: Only zones from the selected home will be used to create climate entities, sensors, and switches. If you need to integrate multiple homes, you can add the integration multiple times, selecting a different home each time.

#### Scan Interval
- **Default**: 300 seconds (5 minutes)
- **Range**: 60-3600 seconds
- **Description**: Controls how frequently the integration polls the EPH Controls API via HTTP to refresh zone states. Lower values provide more frequent updates but increase API usage. MQTT messages provide real-time updates regardless of this setting.

#### Gas Consumption Rate
- **Default**: 1.5 m³/hour
- **Range**: 0.1-10.0 m³/hour
- **Description**: The gas consumption rate of your boiler. This value is used to calculate cumulative gas consumption based on heating duration. The value should match your boiler's specifications (e.g., a typical condensing boiler might consume 1.0-2.0 m³/hour at full power).

### Via YAML (Legacy)
```yaml
climate:
  - platform: ephember
    username: YOUR_USERNAME
    password: YOUR_PASSWORD
```

## Entities and Sensors

### Main Device: "EPH Controls Ember"

The integration creates one main device that provides system-wide monitoring and control. This device includes **7 sensors**:

1. **MQTT Connection** (`sensor.eph_controls_ember_mqtt_connection`)
   - Shows the connection status to the EPH Controls MQTT broker
   - States: `connected` or `disconnected`
   - Updates in real-time via MQTT

2. **Last MQTT Sent** (`sensor.eph_controls_ember_last_mqtt_sent`)
   - Timestamp of the last MQTT message sent to the broker
   - Useful for debugging communication issues

3. **Last MQTT Received** (`sensor.eph_controls_ember_last_mqtt_received`)
   - Timestamp of the last MQTT message received from the broker
   - Shows when the system last received real-time updates

4. **Last HTTP Request** (`sensor.eph_controls_ember_last_http_request`)
   - Timestamp of the last HTTP API request
   - Updated according to the Scan Interval configuration

5. **Heating** (`sensor.eph_controls_ember_heating`)
   - System-wide heating state indicator
   - States: `idle` (no zones heating) or `heating` (at least one zone heating)
   - Updates instantly via MQTT when any zone starts or stops heating

6. **Heating Duration** (`sensor.eph_controls_ember_heating_duration`)
   - Tracks daily heating time in hours
   - Increments gradually every minute while heating is active
   - Resets to 0 at midnight local time
   - Uses the system-wide heating sensor to track when heating is active

7. **Gas Consumption** (`sensor.eph_controls_ember_gas_consumption`)
   - Tracks cumulative gas consumption in cubic meters (m³)
   - Calculated from heating duration multiplied by the configured gas consumption rate
   - Never resets (continuously increasing counter)
   - Updates every minute while heating is active
   - State class: `total_increasing` (suitable for energy monitoring)
   - Requires the "Gas Consumption Rate" configuration parameter

### Zone Devices

Each heating zone (e.g., "Downstairs", "Upstairs", "Hot Water") appears as a separate device with the following entities:

#### Climate Entity
- **Entity Type**: Climate device
- **Controls**:
  - **Temperature Setpoint**: Adjust the target temperature for the zone
  - **HVAC Mode**: Switch between `OFF` and `ON` (Heat) modes
  - **Preset Mode**: Select from the following presets:
    - **None**: Manual mode (ON without schedule)
    - **Auto**: Automatic schedule mode (follows weekly schedule)
    - **All Day**: Continuous heating mode (available for device types 2, 4, and 514)
    - **Boost**: Rapid heating mode (temporary boost)
  - **Current Temperature**: Displays the current room temperature
  - **HVAC Action**: Shows whether the zone is currently `idle` or `heating`
- **Schedule Information**: Weekly heating schedules are provided as read-only attributes on the climate entity. The `schedule` attribute contains the weekly schedule with periods (P1, P2, P3) for each day of the week. To visualize schedules in the Home Assistant UI, you can use the [EPH Schedule Card](https://github.com/UtzR/eph-schedule-card), a custom Lovelace card that displays the weekly schedule in a user-friendly format.

#### Heating Sensor
- **Entity**: `sensor.<zone_name>_heating`
- **States**: `idle` or `heating`
- **Description**: Per-zone heating state indicator that shows whether this specific zone's boiler is currently active
- **Updates**: Instantly via MQTT when the zone's heating state changes

## Supported System Types, Device Types and Zone Device Types

Note that a system is identified via a Sytem Type (EMBER-PS, EMBER-TS1, ...) and a Device Type (we have seen 1, 2, 3, 4). Then each attached zone device (Room or water thermostat) is also identified using a Zone Device Type (we have seen 2, 4, 258, 514, 773).   

### Supported System Types and Device Types

The following System Types have support if the Device Type is above 1. Some older installations use a Device Type 1 and it seems some required API calls are not supported by the EPH cloud. 

| Name      | Mode | Setpoint-R | Setpoint-W | Temp-R| Boost | Boiler| Comment    
|-----------|----- |------------|------------|-------|-------|-------|-------|
| EMBER-PS  | ✅   |  ✅         |  ✅        |  ✅   | ✅     | ✅    |       |
| EMBER-PS2 | ✅   |  🔺         |  ✅        |  ✅   | ✅     | ✅    | Setpoint problem in All-Day mode |
| EMBER-TS1 | ❓   |  ❓         |  ❓        |  ❓   | ❓     | ❓    |       |
| EMBER-TS2 | ✅   |  ✅         |  ✅        |  ✅   | ✅     | ✅    |       |
| EMBER-RS  | ❓   |  ❓         |  ❓        |  ❓   | ❓     | ❓    |       |

✅ This was confirmed to work; 🔺 Works partly; ❓ Might work, confirmation needed

- Mode: Can switch HVAC mode (OFF/ON) and preset modes (Auto, All Day, Boost, None)
- Setpoint-R: Can read the setpoint
- Setpoint-W: Can write the setpoint
- Temp-R: Can read the temperature
- Boost: Boost preset mode is working
- Boiler: Boiler state is reported (Idle/Heating)

### Supported Zone Device Types

| deviceType | Description | Product  (example combination)          |  System Type |  
|------------|-------------|-----------------------------------------|-----------|
| 2          | Thermostat  | Thermostat (RFR) on an RX7-RF           | EMBER-PS  | 
| 4          | Hot Water   | Hot Water (RFC) Controller on an RX7-RF | EMBER-PS  |
| 2          | Thermostat  | Thermostat (RFRP-OT) on an RF1A-OT      | EMBER-TS1 | 
| 258        | Thermostat  | Thermostat (RFRP-OT) on an RF1A-OT      | EMBER-TS2 |
| 514        | Thermostat  | Thermostat (RFR-v2) on an RX7-RF-V2     | EMBER-PS2 |
| 773        | TRV         | TRV (eTRV) on an RF16?                  | EMBER-RS  |

## Differences from Core Integration

A larger number of EPH devices are supported. MQTT communication is added. UI configuration is added. 

## Requirements

- Home Assistant 2025.1.0 or newer

**Note**: This integration uses a modified `pyephember2` library installed within the custom_components folder.


## Credits

- Original integration by [@ttroy50](https://github.com/ttroy50)
- pyephember2 library maintainers and [@roberty99](https://github.com/roberty99)
- Enhanced by [@UtzR](https://github.com/UtzR)

## License

This project is licensed under the same terms as Home Assistant Core.


