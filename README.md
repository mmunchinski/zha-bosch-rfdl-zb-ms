# Bosch RFDL-ZB-MS ZHA Quirk

> **Status:** PR [#4593](https://github.com/zigpy/zha-device-handlers/pull/4593) submitted to [zha-device-handlers](https://github.com/zigpy/zha-device-handlers) — all CI checks passing, awaiting maintainer review.
> Once merged, this repo will be archived and users should use the official version.

A [ZHA device handler (quirk)](https://github.com/zigpy/zha-device-handlers) for the **Bosch RFDL-ZB-MS RADION TriTech** motion sensor.

## Problem

The Bosch RFDL-ZB-MS motion sensor uses the IAS Zone cluster (0x0500) for motion detection. When used with Home Assistant's ZHA integration, the sensor intermittently fails to send motion clear events, causing automations (like turning off lights) to malfunction.

## Solution

This quirk adds a **virtual Occupancy Sensing cluster (0x0406)** that provides a guaranteed software-based motion timeout. When motion is detected:

1. The occupancy sensor turns **on**
2. A 120-second timer starts (reset on each new motion event)
3. After 120 seconds of no motion, occupancy turns **off** (guaranteed)

This creates a reliable motion clear event regardless of hardware behavior.

### Stuck Motion Detection

The sensor can also get "stuck" in a motion-detected state, failing to send clear events. When this happens, the next physical motion triggers a clear event but not a new detect event. The quirk handles this intelligently:

| Software State | Clear Event Behavior |
|----------------|---------------------|
| Not occupied | Treated as motion (stuck sensor finally resetting) |
| Occupied 30+ seconds | Treated as motion (stuck sensor resetting) |
| Occupied < 30 seconds | Ignored (normal quick cycle, software timer handles) |

**Why this works:**
- Normal sensors send motion→clear within ~3-4 seconds
- If a clear arrives after 30+ seconds, the sensor was likely stuck
- If a clear arrives when we're not occupied, the sensor was stuck overnight and is now resetting due to new activity

This ensures that walking past a "stuck" sensor will trigger your automations correctly, even if the sensor has been stuck for hours.

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

### 4. Restart Home Assistant

```bash
ha core restart
```

### 5. Re-pair the sensor

For the quirk to fully apply and create the new occupancy entity:

1. Remove the sensor from ZHA (Settings → Devices → Bosch RFDL-ZB-MS → Delete)
2. Put the sensor in pairing mode
3. Add the device in ZHA

## Entities Created

After pairing with the quirk applied, you'll have two binary sensors:

| Entity | Cluster | Use Case |
|--------|---------|----------|
| `binary_sensor.<name>` | IAS Zone (0x0500) | Instant motion detection |
| `binary_sensor.<name>_occupancy` | Occupancy (0x0406) | **Recommended for automations** - guaranteed 120s timeout |

## Configuration

To change the timeouts, edit `bosch_tritech.py`:

```python
MOTION_TIMEOUT_S = 120           # Occupancy clear timeout (seconds)
STUCK_MOTION_THRESHOLD_S = 30    # Stuck sensor detection threshold (seconds)
```

Then restart Home Assistant.

## Verification

Check logs for quirk activity:

```bash
ha core logs | grep -i "bosch"
```

You should see debug messages when motion is detected and cleared.

## Contributing

This quirk is intended for contribution to [zha-device-handlers](https://github.com/zigpy/zha-device-handlers). To contribute:

1. Fork the zha-device-handlers repository
2. Add the quirk to `zhaquirks/bosch/` directory
3. Submit a pull request

## License

This project follows the same license as [zha-device-handlers](https://github.com/zigpy/zha-device-handlers).
