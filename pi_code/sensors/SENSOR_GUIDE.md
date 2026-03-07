# BNO055 IMU Sensor Guide

## Overview
BNO055 9-DOF IMU connected via I2C. Provides magnetic heading (degrees from magnetic north).

## Hardware
- **Sensor**: BNO055 (chip ID 0xA0 variant)
- **I2C Address**: `0x29` (ADR pin pulled high)
- **I2C Bus**: `/dev/i2c-1`

## Pi Setup (one-time)

### 1. Enable I2C
Uncomment in `/boot/firmware/config.txt`:
```
dtparam=i2c_arm=on
```

### 2. Load i2c-dev module at boot
```bash
echo 'i2c-dev' | sudo tee /etc/modules-load.d/i2c.conf
```

### 3. Install i2c-tools (optional, for debugging)
No internet on the Pi network — download debs on a connected machine and scp them:
```bash
# On connected machine (arm64 Pi 4):
curl -LO http://deb.debian.org/debian/pool/main/i/i2c-tools/libi2c0_4.4-2_arm64.deb
curl -LO http://deb.debian.org/debian/pool/main/i/i2c-tools/i2c-tools_4.4-2_arm64.deb
scp *.deb wadar1@<pi-ip>:~/

# On Pi:
sudo dpkg -i ~/libi2c0_4.4-2_arm64.deb ~/i2c-tools_4.4-2_arm64.deb
```

For armhf (Pi 3), use `_armhf.deb` variants instead.

### 4. Reboot
```bash
sudo reboot
```

### 5. Verify
```bash
ls /dev/i2c-1                  # should exist
sudo i2cdetect -y 1            # should show 0x29
```

## Dependencies
- `smbus2` — pure Python I2C library, installed via apt (`python3-smbus2`)
- No pip needed

## Usage
```bash
python3 read_bno055.py
```

Output:
```
Heading: 202.50  Roll:  -0.31  Pitch: -178.62  | Cal sys=0 gyro=3 accel=0 mag=3
```

**Heading** is degrees from magnetic north (0-360).

## Calibration
Calibration resets each power cycle. Each value ranges 0 (uncalibrated) to 3 (fully calibrated).

| Sensor | How to calibrate |
|--------|-----------------|
| **Gyro** | Leave sensor completely still on a flat surface (~3 seconds) |
| **Mag** | Move sensor in figure-8 motions in the air |
| **Accel** | Place sensor in 6 positions (each face up) for a few seconds each |
| **Sys** | Reaches 3 once other sensors are calibrated |

For heading accuracy, **mag=3** is the important one.

## Files
- `bno055.py` — Driver class (register map, init, read methods)
- `read_bno055.py` — Prints heading/roll/pitch + calibration in a loop

## Wiring (I2C)
| BNO055 Pin | Pi Pin |
|-----------|--------|
| VIN | 3.3V (pin 1) |
| GND | GND (pin 6) |
| SDA | SDA1 (pin 3 / GPIO 2) |
| SCL | SCL1 (pin 5 / GPIO 3) |
