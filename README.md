# Bosch RFDL-ZB-MS ZHA Quirk

> **Status:** PR [#4593](https://github.com/zigpy/zha-device-handlers/pull/4593) submitted to [zha-device-handlers](https://github.com/zigpy/zha-device-handlers) — awaiting maintainer review.
> Once merged, this repo will be archived and users should use the official version.

A [ZHA device handler (quirk)](https://github.com/zigpy/zha-device-handlers) for the **Bosch RFDL-ZB-MS RADION TriTech** motion sensor.

## Background

These sensors were originally designed for iControl commercial security systems. When repurposed for consumer Zigbee networks with Home Assistant ZHA, they exhibit reliability issues that this quirk addresses.

## Problems Addressed

1. **Intermittent motion clear failures** - The IAS Zone cluster sometimes fails to send motion clear events, breaking automations
2. **Stuck sensor states** - Sensors can get "stuck" in motion-detected state for hours
3. **Silent failures** - Devices can stop communicating without any indication
4. **Long poll intervals** - Default check-in intervals are too long to detect issues quickly

## Solution

This quirk provides:

### 1. Virtual Occupancy Cluster
A software-based motion timeout that **guarantees** clear events:
- Occupancy turns **on** when motion detected
- 120-second timer starts (reset on each new motion)
- After 120 seconds of no motion, occupancy turns **off** (guaranteed)

### 2. Startup State Recovery
Clears stale occupancy states on Home Assistant restart:
- 5-second delayed clear after initialization
- Overrides HA's state restoration from database
- Only clears if no real motion was detected since startup
- Prevents sensors from being stuck "detected" after restarts

### 3. Aggressive Poll Control
Configures devices to check in more frequently:
- 15-minute check-in interval (vs default hours)
- Faster detection of offline/stuck devices

### 4. Communication Health Monitoring
- Tracks last communication time from each device
- Periodic stuck state checks (every 5 minutes)
- Warns if device occupied 30+ minutes without new motion
- Warns if no communication received for 1+ hour
- Counts all events for diagnostics

### 5. Enhanced IAS Zone Logging
- Parses all zone status bits (motion, tamper, low_battery, supervision)
- Immediate warnings on tamper or low battery conditions
- Event counting for diagnostics

## Device Signature

```json
{
  "manufacturer": "Bosch",
  "model": "RFDL-ZB-MS",
  "endpoints": {
    "1": {
      "profile_id": "0x0104",
      "device_type": "0x0402",
      "input_clusters": ["0x0000", "0x0001", "0x0003", "0x0020", "0x0400", "0x0402", "0x0500", "0x0b05"],
      "output_clusters": ["0x0019"]
    }
  }
}
```

## Installation

### 1. Create the quirks directory

```bash
mkdir -p /config/custom_zha_quirks
```

### 2. Copy the quirk files

Copy the contents of `custom_zha_quirks/` to `/config/custom_zha_quirks/`:
- `__init__.py`
- `bosch_tritech.py`

### 3. Configure Home Assistant

Add to `configuration.yaml`:

```yaml
zha:
  custom_quirks_path: /config/custom_zha_quirks/
```

### 4. Enable debug logging (recommended)

Add to `configuration.yaml` to see quirk activity:

```yaml
logger:
  default: warning
  logs:
    custom_zha_quirks: debug
    zhaquirks: debug
```

### 5. Restart Home Assistant

```bash
ha core restart
```

### 6. Re-pair or reconfigure sensors

For the quirk to fully apply:

**Option A - Reconfigure (preserves entity IDs):**
1. Go to Settings → Devices → [Your Bosch Sensor]
2. Click "Reconfigure Device"

**Option B - Re-pair (creates new entities):**
1. Remove the sensor from ZHA
2. Factory reset sensor (hold tamper button, insert battery, hold 10+ seconds)
3. Add the device in ZHA

## Entities Created

After pairing with the quirk applied:

| Entity | Cluster | Use Case |
|--------|---------|----------|
| `binary_sensor.<name>` | IAS Zone (0x0500) | Instant motion detection |
| `binary_sensor.<name>_occupancy` | Occupancy (0x0406) | **Recommended for automations** - guaranteed 120s timeout |

## Configuration

To change the timeouts, edit `bosch_tritech.py`:

```python
MOTION_TIMEOUT_S = 120           # Occupancy clear timeout (seconds)
STUCK_WARNING_THRESHOLD_S = 1800 # Warn if occupied this long without new events (30 min)
CHECKIN_INTERVAL = 3600          # Poll control check-in (quarter-seconds, 3600 = 15 min)
```

Then restart Home Assistant.

## Verification

Check logs for quirk activity:

```bash
grep "Bosch RFDL-ZB-MS" /config/home-assistant.log | tail -50
```

You should see messages like:
```
Bosch RFDL-ZB-MS [00:0d:6f:00:10:86:cc:91]: Motion #5 detected, starting 120s timer
Bosch RFDL-ZB-MS [00:0d:6f:00:10:86:cc:91]: Zone status 0x0021 - motion=True, tamper=False, low_battery=False
Bosch RFDL-ZB-MS [00:0d:6f:00:10:86:cc:91]: Clearing occupancy after 120s timeout
```

## Troubleshooting

### Device not responding (red flashing LED)
1. Remove battery for 30+ seconds
2. If that doesn't work, factory reset: hold tamper button while inserting battery for 10+ seconds
3. Re-pair to ZHA

### Motion events not triggering automations
1. Use the `_occupancy` entity, not the base IAS Zone entity
2. Check logs for quirk messages
3. Verify quirk is loaded: device should show custom clusters in ZHA

### Device goes offline frequently
1. Add Zigbee router devices nearby (smart plugs)
2. Check battery (even if percentage shows OK)
3. Reduce interference from USB 3.0 / WiFi

## Contributing

This quirk is intended for contribution to [zha-device-handlers](https://github.com/zigpy/zha-device-handlers).

- **PR #4593**: https://github.com/zigpy/zha-device-handlers/pull/4593
- Fork: https://github.com/mmunchinski/zha-device-handlers

## License

Apache 2.0 - Same as [zha-device-handlers](https://github.com/zigpy/zha-device-handlers).
