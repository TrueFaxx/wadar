#!/usr/bin/env python3
"""Simple HTTP server that exposes BNO055 sensor data as JSON."""

import json
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from bno055 import BNO055

imu = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return

        try:
            heading, roll, pitch = imu.euler()
            sys, gyro, accel, mag = imu.calibration_status()
            data = {
                "heading": round(heading, 2),
                "roll": round(roll, 2),
                "pitch": round(pitch, 2),
                "cal_sys": sys,
                "cal_gyro": gyro,
                "cal_accel": accel,
                "cal_mag": mag,
                "time": time.time(),
            }
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
    global imu
    print("Initializing BNO055...")
    imu = BNO055()
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
