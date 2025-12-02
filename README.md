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

### ✅ Improved State Updates
- Immediate state refresh after changing preset, mode, or temperature
- Clears API cache to ensure fresh data

### ✅ Network Error Handling
- Graceful handling of API timeouts and network errors
- Prevents log spam during temporary connectivity issues

### ✅ pyephember2 Library Bug Fix
- Fixes a bug in pyephember2 v0.4.12 where `ZoneCommand` is called with missing arguments
- Monkey-patches the broken `_set_zone_boost` method

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

| Device Type | Model Name |
|-------------|------------|
| 2 | Thermostat |
| 4 | Hot Water Controller |
| 514 | Hot Water Controller |
| 773 | Thermostatic Radiator Valve |

## Differences from Core Integration

| Feature | Core Integration | This Integration |
|---------|-----------------|------------------|
| Boost mode | ❌ Not supported | ✅ Via preset modes |
| UI configuration | ❌ YAML only | ✅ Full UI support |
| Device registry | ❌ Entities only | ✅ Devices with entities |
| State refresh | ❌ Polling only | ✅ Immediate after changes |
| Error handling | ❌ Basic | ✅ Graceful timeout handling |
| pyephember2 bug | ❌ Affected | ✅ Fixed via monkey-patch |

## Requirements

- Home Assistant 2024.1 or newer
- pyephember2 v0.4.12

## Known Issues

- The pyephember2 library (v0.4.12) has a bug in the boost functionality. This integration includes a workaround, but the upstream library should be fixed.

## Credits

- Original integration by [@ttroy50](https://github.com/ttroy50)
- pyephember2 library maintainers
- Enhanced by [@UtzR](https://github.com/UtzR)

## License

This project is licensed under the same terms as Home Assistant Core.

