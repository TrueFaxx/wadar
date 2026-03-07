#!/usr/bin/env python3
"""Read and print BNO055 sensor data in a loop."""

import time
from bno055 import BNO055


def main():
    print("Initializing BNO055...")
    imu = BNO055()
    print("BNO055 ready.\n")

    try:
        while True:
            try:
                heading, roll, pitch = imu.euler()
                sys, gyro, accel, mag = imu.calibration_status()
            except OSError:
                time.sleep(0.05)
                continue

            print(
                f"Heading: {heading:7.2f}  Roll: {roll:7.2f}  Pitch: {pitch:7.2f}  "
                f"| Cal sys={sys} gyro={gyro} accel={accel} mag={mag}",
                flush=True,
            )
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        imu.close()


if __name__ == "__main__":
    main()
