"""BNO055 9-DOF IMU driver using smbus2 over I2C."""

import json
import os
import time
from smbus2 import SMBus

# Default I2C address (0x29 if ADR pin pulled high)
BNO055_ADDRESS = 0x29

# Chip identification
BNO055_CHIP_ID = 0xA5
BNO055_CHIP_ID_ALT = 0xA0  # Some revisions/clones report 0xA0

# Register addresses
REG_CHIP_ID = 0x00
REG_PAGE_ID = 0x07
REG_SYS_TRIGGER = 0x3F
REG_PWR_MODE = 0x3E
REG_OPR_MODE = 0x3D
REG_UNIT_SEL = 0x3B
REG_SYS_STATUS = 0x39
REG_SYS_ERR = 0x3A
REG_CALIB_STAT = 0x35
REG_TEMP = 0x34
REG_CALIB_DATA = 0x55  # 22 bytes of calibration data

# Data registers
REG_EUL_HEADING = 0x1A  # 2 bytes, LSB first
REG_EUL_ROLL = 0x1C
REG_EUL_PITCH = 0x1E

REG_QUA_W = 0x20
REG_QUA_X = 0x22
REG_QUA_Y = 0x24
REG_QUA_Z = 0x26

REG_ACC_X = 0x08
REG_ACC_Y = 0x0A
REG_ACC_Z = 0x0C

REG_GYR_X = 0x14
REG_GYR_Y = 0x16
REG_GYR_Z = 0x18

REG_MAG_X = 0x0E
REG_MAG_Y = 0x10
REG_MAG_Z = 0x12

# Operation modes
MODE_CONFIG = 0x00
MODE_NDOF = 0x0C
MODE_IMU = 0x08
MODE_COMPASS = 0x09
MODE_M4G = 0x0A
MODE_NDOF_FMC_OFF = 0x0B

# Power modes
POWER_NORMAL = 0x00


class BNO055:
    I2C_RETRIES = 5

    def __init__(self, bus_num=1, address=BNO055_ADDRESS, heading_offset=0.0):
        self.bus = SMBus(bus_num)
        self.address = address
        self.heading_offset = heading_offset
        self._verify_chip_id()
        self._configure()

    def _i2c_read(self, register, length):
        for attempt in range(self.I2C_RETRIES):
            try:
                return self.bus.read_i2c_block_data(self.address, register, length)
            except OSError:
                if attempt == self.I2C_RETRIES - 1:
                    raise
                time.sleep(0.01 * (attempt + 1))

    def _verify_chip_id(self):
        chip_id = self.bus.read_byte_data(self.address, REG_CHIP_ID)
        if chip_id not in (BNO055_CHIP_ID, BNO055_CHIP_ID_ALT):
            raise RuntimeError(
                f"BNO055 not found. Expected chip ID 0x{BNO055_CHIP_ID:02X} "
                f"or 0x{BNO055_CHIP_ID_ALT:02X}, got 0x{chip_id:02X}"
            )

    def _configure(self):
        # Switch to config mode
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_CONFIG)
        time.sleep(0.025)

        # Reset
        self.bus.write_byte_data(self.address, REG_SYS_TRIGGER, 0x20)
        time.sleep(0.65)

        # Wait for chip ID to be readable after reset
        for _ in range(50):
            try:
                if self.bus.read_byte_data(self.address, REG_CHIP_ID) in (BNO055_CHIP_ID, BNO055_CHIP_ID_ALT):
                    break
            except OSError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("BNO055 did not come back after reset")

        # Normal power mode
        self.bus.write_byte_data(self.address, REG_PWR_MODE, POWER_NORMAL)
        time.sleep(0.01)

        # Page 0
        self.bus.write_byte_data(self.address, REG_PAGE_ID, 0x00)

        # Units: degrees, m/s^2, dps, celsius
        self.bus.write_byte_data(self.address, REG_UNIT_SEL, 0x00)

        # Set to NDOF mode (full sensor fusion)
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_NDOF)
        time.sleep(0.02)

    def _read_signed_16(self, register):
        data = self._i2c_read(register, 2)
        value = data[0] | (data[1] << 8)
        if value >= 0x8000:
            value -= 0x10000
        return value

    def _read_vector(self, register, scale=1.0):
        data = self._i2c_read(register, 6)
        x = (data[0] | (data[1] << 8))
        y = (data[2] | (data[3] << 8))
        z = (data[4] | (data[5] << 8))
        # Sign extend
        if x >= 0x8000: x -= 0x10000
        if y >= 0x8000: y -= 0x10000
        if z >= 0x8000: z -= 0x10000
        return (x / scale, y / scale, z / scale)

    def euler(self):
        """Returns (heading, roll, pitch) in degrees."""
        data = self._i2c_read(REG_EUL_HEADING, 6)
        heading = (data[0] | (data[1] << 8))
        roll = (data[2] | (data[3] << 8))
        pitch = (data[4] | (data[5] << 8))
        if heading >= 0x8000: heading -= 0x10000
        if roll >= 0x8000: roll -= 0x10000
        if pitch >= 0x8000: pitch -= 0x10000
        # Euler angles are in units of 1/16 degree
        heading = (heading / 16.0 + self.heading_offset) % 360
        return (heading, roll / 16.0, pitch / 16.0)

    def quaternion(self):
        """Returns (w, x, y, z) quaternion. Values are unitless, scaled by 2^14."""
        data = self._i2c_read(REG_QUA_W, 8)
        w = (data[0] | (data[1] << 8))
        x = (data[2] | (data[3] << 8))
        y = (data[4] | (data[5] << 8))
        z = (data[6] | (data[7] << 8))
        if w >= 0x8000: w -= 0x10000
        if x >= 0x8000: x -= 0x10000
        if y >= 0x8000: y -= 0x10000
        if z >= 0x8000: z -= 0x10000
        scale = 1 << 14  # 2^14 = 16384
        return (w / scale, x / scale, y / scale, z / scale)

    def accelerometer(self):
        """Returns (x, y, z) acceleration in m/s^2."""
        return self._read_vector(REG_ACC_X, 100.0)

    def gyroscope(self):
        """Returns (x, y, z) angular velocity in degrees/sec."""
        return self._read_vector(REG_GYR_X, 16.0)

    def magnetometer(self):
        """Returns (x, y, z) magnetic field in microtesla."""
        return self._read_vector(REG_MAG_X, 16.0)

    def calibration_status(self):
        """Returns (sys, gyro, accel, mag) calibration values, each 0-3."""
        status = self.bus.read_byte_data(self.address, REG_CALIB_STAT)
        sys = (status >> 6) & 0x03
        gyro = (status >> 4) & 0x03
        accel = (status >> 2) & 0x03
        mag = status & 0x03
        return (sys, gyro, accel, mag)

    def temperature(self):
        """Returns temperature in celsius."""
        return self.bus.read_byte_data(self.address, REG_TEMP)

    def system_status(self):
        """Returns (status, error) codes."""
        status = self.bus.read_byte_data(self.address, REG_SYS_STATUS)
        error = self.bus.read_byte_data(self.address, REG_SYS_ERR)
        return (status, error)

    def get_calibration(self):
        """Read 22-byte calibration profile from sensor."""
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_CONFIG)
        time.sleep(0.025)
        data = self._i2c_read(REG_CALIB_DATA, 22)
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_NDOF)
        time.sleep(0.02)
        return data

    def set_calibration(self, data):
        """Write 22-byte calibration profile to sensor."""
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_CONFIG)
        time.sleep(0.025)
        for i, val in enumerate(data):
            self.bus.write_byte_data(self.address, REG_CALIB_DATA + i, val)
        self.bus.write_byte_data(self.address, REG_OPR_MODE, MODE_NDOF)
        time.sleep(0.02)

    def save_calibration(self, path="calibration.json"):
        """Save calibration profile to file."""
        data = self.get_calibration()
        with open(path, "w") as f:
            json.dump(data, f)

    def load_calibration(self, path="calibration.json"):
        """Load calibration profile from file. Returns True if loaded."""
        if not os.path.exists(path):
            return False
        with open(path) as f:
            data = json.load(f)
        self.set_calibration(data)
        return True

    def close(self):
        self.bus.close()
