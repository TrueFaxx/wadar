#!/usr/bin/env python3
"""Simple HTTP server that exposes BNO055 sensor data as JSON."""

import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from bno055 import BNO055

imu = None
cal_saved = False
CAL_PATH = None


zero_count = 0


def reinit_imu():
    """Re-initialize BNO055 after a crash."""
    global imu, cal_saved, zero_count
    try:
        imu.close()
    except Exception:
        pass
    offset = imu.heading_offset
    imu = BNO055(heading_offset=offset)
    if imu.load_calibration(CAL_PATH):
        print("Reloaded calibration after reinit", flush=True)
    cal_saved = False
    zero_count = 0
    print("BNO055 re-initialized", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global cal_saved, zero_count
        if self.path != "/":
            self.send_error(404)
            return

        try:
            heading, roll, pitch = imu.euler()
            s, gyro, accel, mag = imu.calibration_status()

            # Detect chip crash: all zeros for 5+ consecutive reads
            if heading == 0 and roll == 0 and pitch == 0 and s == 0 and mag == 0:
                zero_count += 1
                if zero_count >= 5:
                    print("BNO055 crash detected (all zeros), reinitializing...", flush=True)
                    try:
                        reinit_imu()
                        heading, roll, pitch = imu.euler()
                        s, gyro, accel, mag = imu.calibration_status()
                    except Exception as e:
                        data = {"error": f"reinit failed: {e}"}
            else:
                zero_count = 0

            data = {
                "heading": round(heading, 2),
                "roll": round(roll, 2),
                "pitch": round(pitch, 2),
                "cal_sys": s,
                "cal_gyro": gyro,
                "cal_accel": accel,
                "cal_mag": mag,
                "time": time.time(),
            }
            if not cal_saved and mag == 3 and gyro == 3:
                imu.save_calibration(CAL_PATH)
                cal_saved = True
        except OSError:
            data = {"error": "I2C read failed"}

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass  # suppress request logs


def main():
    global imu, CAL_PATH
    offset = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    CAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
    print(f"Initializing BNO055 (heading offset: {offset})...")
    imu = BNO055(heading_offset=offset)
    if imu.load_calibration(CAL_PATH):
        print(f"Loaded calibration from {CAL_PATH}")
    else:
        print("No saved calibration found, will save when calibrated.")
    print("BNO055 ready.")

    server = HTTPServer(("0.0.0.0", 8080), Handler)
    print("Serving on port 8080")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        imu.close()
        server.server_close()


if __name__ == "__main__":
    main()
