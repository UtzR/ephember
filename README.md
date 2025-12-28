# EPH Controls Ember - Enhanced Home Assistant Integration

A custom Home Assistant integration for EPH Controls Ember heating systems with enhanced functionality beyond the core Home Assistant integration.

## Features

This integration provides the following enhancements over the [original Home Assistant ephember integration](https://www.home-assistant.io/integrations/ephember/):

### ✅ Boost Mode Support
- Activate and deactivate boost via preset modes (`boost` / `none`)
- Boost state is reflected in the UI immediately

### ✅ UI Configuration
- Configure the integration via Home Assistant UI (Settings → Devices & Services → Add Integration)
- No need to edit `configuration.yaml`
- Validates credentials during setup

### ✅ Device Support
- Each zone appears as a device in Home Assistant
- Devices show manufacturer (EPH Controls) and model type
- Human-readable model names (Thermostat, Hot Water Controller, etc.)

### ✅ Improved State Updates and MQTT support
- State refresh after changing preset, mode, or temperature
- Clears API cache to ensure fresh data
- Uses MQTT for improved state synchronization

### ✅ Network Error Handling
- Graceful handling of API timeouts and network errors
- Prevents log spam during temporary connectivity issues

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

### Via YAML (Legacy)
```yaml
climate:
  - platform: ephember
    username: YOUR_USERNAME
    password: YOUR_PASSWORD
```

## Supported Device Types

| deviceType | Description | Product                           |  Name     |  
|------------|-------------|-----------------------------------|-----------|
| 2          | Thermostat  | Thermostat on an RX7-RF           | EMBER-PS  | 
| 4          | Hot Water   | Hot Water Controller on an RX7-RF | EMBER-PS  |
| 258        | Thermostat  | Thermostat on an RF1A-OT          | EMBER-TS2 |
| 514        | Thermostat  | Thermostat on an RX7-RF-V2        | EMBER-PS2 |
| 773        | TRV         | TRV on an RF16?                   | ???       |


| Name      | Comment    
|-----------|--------------|
| EMBER-PS  | Working: Mode switching (on/off/auto); setting setpoint; reporting temp and setpoint; boost on/off; reporting boiler state | 
| EMBER-TS2 | Needs testing: Mode switching (on/off/auto); setting setpoint; reporting temp and setpoint; boost on/off; reporting boiler state | 
| EMBER-PS2 | Working: reporting temp and setpoint; Needs testing: Mode switching (on/off/auto); setting setpoint; reporting boiler state | 

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


