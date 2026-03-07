# WiFi Monitor Mode Capture Guide

## Overview
RTL8821AU USB adapter (TP-Link Archer T2U PLUS) in monitor mode, capturing beacons, probe responses, and probe requests via tcpdump. Serves RSSI + AP/device data as JSON over HTTP.

## Hardware
- **Adapter**: TP-Link Archer T2U PLUS (RTL8821AU, USB ID `2357:0120`)
- **Driver**: `88XXau` from [aircrack-ng/rtl8812au](https://github.com/aircrack-ng/rtl8812au) v5.6.4.2
- **Interface**: `wlan1` (USB adapter) — `wlan0` stays connected to G79-PRIVATE

## Pi Setup

### 1. Build & install driver
The driver tarball is pre-downloaded (stripped of android/docs to ~3.6MB). Build on each Pi:
```bash
sudo bash ~/wifi/build_driver.sh
```
This auto-detects architecture (arm64 for Pi 4, armhf for Pi 3), compiles, installs the kernel module, and loads it.

### 2. Install tcpdump
No internet on Pis — download debs on a connected machine and transfer:
```bash
# arm64 (Pi 4):
curl -LO https://deb.debian.org/debian/pool/main/t/tcpdump/tcpdump_4.99.5-2_arm64.deb

# armhf (Pi 3):
curl -LO https://deb.debian.org/debian/pool/main/t/tcpdump/tcpdump_4.99.5-2_armhf.deb

# On Pi:
sudo dpkg -i ~/tcpdump_*.deb
```

### 3. Verify
```bash
lsmod | grep 88XXau          # driver loaded
iw dev                        # wlan1 should appear
sudo tcpdump -i wlan1 -c 5   # captures frames
```

## Usage
```bash
sudo python3 ~/wifi/monitor_capture.py [interface] [port] [channel]
```

| Arg | Default | Description |
|-----|---------|-------------|
| interface | `wlan1` | WiFi interface to put in monitor mode |
| port | `8081` | HTTP server port |
| channel | `6` | Channel to monitor (use `hop` for all channels) |

**WARNING**: Channel hopping (`hop`) disrupts wlan0 connectivity. Use a fixed channel for stable SSH access.

### Example
```bash
# Monitor channel 6 (where G79-PRIVATE lives)
sudo python3 ~/wifi/monitor_capture.py wlan1 8081 6

# Monitor all channels (breaks SSH — use only with serial/keyboard access)
sudo python3 ~/wifi/monitor_capture.py wlan1 8081 hop
```

## HTTP API (port 8081)

### `GET /` — AP summary
All access points sorted by RSSI (strongest first).
```json
{
  "interface": "wlan1",
  "channel": 6,
  "ap_count": 42,
  "device_count": 274,
  "aps": [
    {
      "bssid": "00:18:39:1f:be:7e",
      "ssid": "G79-PRIVATE",
      "rssi": -28,
      "channel": 6,
      "count": 35,
      "age": 1.2
    }
  ],
  "time": 1764870059.25
}
```

### `GET /devices` — Client devices
Devices detected via probe requests, sorted by RSSI.
```json
{
  "device_count": 274,
  "devices": [
    {
      "mac": "2a:ed:a5:c8:bc:f3",
      "rssi": -28,
      "probed_ssids": ["TMobileWingman"],
      "count": 5,
      "age": 2.1
    }
  ]
}
```

### `GET /ap/<BSSID>` — AP RSSI history
Last 60 RSSI samples with timestamps for one AP. Use this for directional drift analysis.
```json
{
  "bssid": "00:18:39:1f:be:7e",
  "ssid": "G79-PRIVATE",
  "rssi": -30,
  "channel": 6,
  "count": 35,
  "rssi_history": [
    {"rssi": -26, "t": 1764870027.08},
    {"rssi": -70, "t": 1764870027.30},
    {"rssi": -28, "t": 1764870027.36}
  ]
}
```

### `GET /device/<MAC>` — Device RSSI history
Same as AP detail but for a client device.

## Signal Strength (RSSI) Reference
| RSSI | Signal Quality |
|------|---------------|
| -20 to -30 dBm | Excellent (very close) |
| -30 to -50 dBm | Good |
| -50 to -70 dBm | Fair |
| -70 to -85 dBm | Weak |
| < -85 dBm | Very weak / noise floor |

As a dish rotates 360 degrees, RSSI for a given AP should swing by 20-40 dB between the peak (dish pointed at AP) and null (dish pointed away).

## Correlating with BNO055 Heading
The sensor server (port 8080) provides magnetic heading. To map signal direction:

1. Query both endpoints simultaneously:
   - `http://<pi>:8080/` — heading, roll, pitch
   - `http://<pi>:8081/ap/<BSSID>` — RSSI history with timestamps
2. Match timestamps between RSSI samples and heading readings
3. Plot RSSI vs heading to see directional pattern
4. Peak RSSI heading = direction to AP

## SSH to Pis
```bash
# Must use -o PubkeyAuthentication=no with sshpass (pubkey negotiation hangs)
sshpass -p raspberry ssh -o PubkeyAuthentication=no wadar1@192.168.1.101  # Pi 4
sshpass -p raspberry ssh -o PubkeyAuthentication=no wadar2@192.168.1.103  # Pi 3
```

## Files
- `build_driver.sh` — Compiles and installs 88XXau driver (auto-detects Pi 3 vs Pi 4)
- `monitor_capture.py` — Monitor mode capture + HTTP JSON server
- `rtl8812au.tar.gz` — Driver source (aircrack-ng/rtl8812au, stripped to 3.6MB)

## Current Deployment
| | Pi 4 (`192.168.1.101`) | Pi 3 (`192.168.1.103`) |
|---|---|---|
| Kernel | 6.12.47+rpt-rpi-v8 (arm64) | 6.12.47+rpt-rpi-v7 (armhf) |
| wlan1 MAC | 98:25:4a:fe:5e:bd | 50:3d:d1:29:dc:b0 |
| Sensor server | :8080 | :8080 |
| WiFi capture | :8081 | :8081 |

## Troubleshooting

### wlan1 not appearing after reboot
Driver doesn't persist across reboots. Re-load with:
```bash
sudo modprobe 88XXau
```
Or re-run `build_driver.sh` if the kernel was updated.

### tcpdump shows 0 beacons
Check the channel — beacons only appear on the channel the AP uses:
```bash
sudo iw dev wlan1 set channel 6
```

### SSH hangs to Pi
The pubkey negotiation with sshpass causes hangs. Always use:
```bash
ssh -o PubkeyAuthentication=no ...
```
