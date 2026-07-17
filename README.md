[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)
[![Device Custom](https://img.shields.io/badge/Branch-device--custom-blue.svg)](https://github.com/jgogoes/hass_ocpp_bg_sync_ev/tree/device-custom)

![OCPP](https://github.com/home-assistant/brands/raw/master/custom_integrations/ocpp/icon.png)

# OCPP Integration for Home Assistant - BG Sync EV Customization

## ⚡ What This Is

This is a **customized version** of the [original OCPP integration by lbbrhzn](https://github.com/lbbrhzn/ocpp), **specifically optimized for BG Sync EV chargers** used in Home Assistant.

### 📌 Quick Facts
- **Based on:** [lbbrhzn/ocpp](https://github.com/lbbrhzn/ocpp) - Home Assistant OCPP integration
- **What it does:** Connects EV chargers to Home Assistant using OCPP protocol
- **Who it's for:** Users with **BG Sync EV chargers** who want better compatibility and stability
- **License:** Same as original (see LICENSE file)

## 🎯 Key Features

This branch **builds on top of** the original integration with device-specific improvements:

✨ **BG Sync-Specific Enhancements:**
- **Enhanced switch state stability** — Fixes permanent stuck states after Home Assistant restart
- **Optimized CT clamp handling** — Reliable sensor updates and entity management
- **Improved HA notifications** — Efficient data transfer notifications to Home Assistant
- **Streamlined configuration** — Simplified Local Authorization List handling
- **Better protocol compatibility** — Enhanced support for BG Sync charger variations

⚡ **Advanced Capabilities:**
- **EVCC Compatible** — Full integration with EVCC for advanced charging automation
- **Cable Release Control** — Auto-unlock switch to prevent cable unplugging unless authorized
  - Protects against accidental disconnection during charging
  - Require explicit Home Assistant confirmation to release cable
- **Brightness Control** — Adjust outdoor charger port LED brightness for visual feedback
  - *Requires Home Assistant automation to control* (e.g., 1% at dusk, 30% at dawn)
  - Integration provides the control, you create the automations
  - Example: Automate brightness based on time, solar production, or charging status
- **Solar Charging Ready** — Reliable integration with EVCC for solar-powered EV charging
  - Consistent power monitoring for grid-aware charging
  - Real-time house current monitoring via CT clamp
  
🔌 **Complete Charger Control:**
- **Charging Control** — Start/stop charging remotely
- **Connector Management** — Availability and status monitoring
- **Current Limiting** — Set max charging current (hardware limits enforced)
- **Overvoltage Protection** — Configurable voltage protection thresholds
- **CT Clamp Monitoring** — House voltage and current sensing for load management
- **Connection Timeouts** — Customizable charger connection timeout settings
- **Meter Intervals** — Configure how often charger reports usage data
- **Real-time Metrics** — Live charging power, current, voltage, temperature, energy

## ❓ Not for BG Sync? Use the Original

If you have a **different charger**, use the [original OCPP integration](https://github.com/lbbrhzn/ocpp):
```bash
# Original project (supports many chargers):
git clone https://github.com/lbbrhzn/ocpp.git
```

## 📦 Installation for BG Sync Users

Clone this specific branch:

```bash
git clone --branch device-custom https://github.com/jgogoes/hass_ocpp_bg_sync_ev.git
# Then copy custom_components/ocpp to your Home Assistant custom_components directory
```

Or via HACS (if enabled for this repo).

## 🔄 Staying Updated

This branch automatically merges updates from the [original project](https://github.com/lbbrhzn/ocpp), so you get:
- Latest bug fixes
- New OCPP protocol support
- Community improvements
- Plus your BG Sync optimizations

## 📚 Documentation

For full documentation on OCPP integration features, see:
- [Original Project Docs](https://home-assistant-ocpp.readthedocs.io)
- [Python OCPP Package](https://github.com/mobilityhouse/ocpp)

## 🙏 Credits

**This work builds on:**
- The incredible [OCPP integration](https://github.com/lbbrhzn/ocpp) by [lbbrhzn](https://github.com/lbbrhzn)
- The [Python OCPP Package](https://github.com/mobilityhouse/ocpp) by Mobility House
- Home Assistant's amazing smart home platform

**Special thanks** to the original maintainers for creating such a solid foundation. This fork would not exist without their excellent work.

## 💪 Support

- **For BG Sync-specific issues:** Use this repository's issues
- **For general OCPP questions:** Check the [original project](https://github.com/lbbrhzn/ocpp)
- **To support the original author:** Consider [buying them a coffee](https://www.buymeacoffee.com/lbbrhzn) ☕

## 📄 License

This project maintains the same license as the original OCPP integration. See LICENSE file for details.
