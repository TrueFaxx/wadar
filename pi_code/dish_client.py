#!/usr/bin/env python3
"""Dish raw data streamer.

Reads BNO055 heading directly over I2C and captures raw WiFi packets
via tcpdump. Streams every packet as (heading, bssid, ssid, rssi, channel)
over a WebSocket server. No smoothing, no aggregation — all processing
happens on the receiving end.

Replaces sensor_server.py + monitor_capture.py.

Usage:
    sudo python3 dish_client.py <dish_id> [port] [heading_offset] [wifi_iface] [channel]

Example:
    sudo python3 dish_client.py DISH-1 8082 0 wlan1 6

Dependencies: smbus2, websockets
Must run as root for monitor mode + tcpdump.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import threading
import queue

import websockets
from websockets.asyncio.server import serve

# ── Config ────────────────────────────────────────────────────────

DISH_ID = "DISH-1"
PORT = 8082
HEADING_OFFSET = 0.0
WIFI_IFACE = "wlan1"
CHANNEL = "6"

# BNO055 I2C
BNO055_ADDRESS = 0x29
REG_CHIP_ID = 0x00
REG_PAGE_ID = 0x07
REG_SYS_TRIGGER = 0x3F
REG_PWR_MODE = 0x3E
REG_OPR_MODE = 0x3D
REG_UNIT_SEL = 0x3B
REG_CALIB_STAT = 0x35
REG_EUL_HEADING = 0x1A
REG_CALIB_DATA = 0x55
MODE_CONFIG = 0x00
MODE_NDOF = 0x0C
POWER_NORMAL = 0x00
BNO055_CHIP_ID = 0xA5
BNO055_CHIP_ID_ALT = 0xA0
I2C_RETRIES = 5
CAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")

# ── Shared state ──────────────────────────────────────────────────

heading_lock = threading.Lock()
current_heading = 0.0

# Queue of raw packets to stream
packet_queue = queue.Queue(maxsize=5000)

# ── BNO055 ────────────────────────────────────────────────────────

_bus = None


def imu_init(offset=0.0):
    global _bus, HEADING_OFFSET
    from smbus2 import SMBus
    HEADING_OFFSET = offset
    _bus = SMBus(1)

    chip_id = _bus.read_byte_data(BNO055_ADDRESS, REG_CHIP_ID)
    if chip_id not in (BNO055_CHIP_ID, BNO055_CHIP_ID_ALT):
        raise RuntimeError(f"BNO055 not found (got 0x{chip_id:02X})")

    _bus.write_byte_data(BNO055_ADDRESS, REG_OPR_MODE, MODE_CONFIG)
    time.sleep(0.025)
    _bus.write_byte_data(BNO055_ADDRESS, REG_SYS_TRIGGER, 0x20)
    time.sleep(0.65)
    for _ in range(50):
        try:
            if _bus.read_byte_data(BNO055_ADDRESS, REG_CHIP_ID) in (BNO055_CHIP_ID, BNO055_CHIP_ID_ALT):
                break
        except OSError:
            pass
        time.sleep(0.05)
    _bus.write_byte_data(BNO055_ADDRESS, REG_PWR_MODE, POWER_NORMAL)
    time.sleep(0.01)
    _bus.write_byte_data(BNO055_ADDRESS, REG_PAGE_ID, 0x00)
    _bus.write_byte_data(BNO055_ADDRESS, REG_UNIT_SEL, 0x00)
    _bus.write_byte_data(BNO055_ADDRESS, REG_OPR_MODE, MODE_NDOF)
    time.sleep(0.02)

    if os.path.exists(CAL_PATH):
        _bus.write_byte_data(BNO055_ADDRESS, REG_OPR_MODE, MODE_CONFIG)
        time.sleep(0.025)
        with open(CAL_PATH) as f:
            cal = json.load(f)
        for i, val in enumerate(cal):
            _bus.write_byte_data(BNO055_ADDRESS, REG_CALIB_DATA + i, val)
        _bus.write_byte_data(BNO055_ADDRESS, REG_OPR_MODE, MODE_NDOF)
        time.sleep(0.02)
        print("Loaded saved calibration", flush=True)

    print(f"BNO055 ready (offset={offset})", flush=True)


def heading_loop():
    """Read heading at ~50Hz, update shared state."""
    global current_heading
    zero_count = 0
    while True:
        try:
            data = None
            for attempt in range(I2C_RETRIES):
                try:
                    data = _bus.read_i2c_block_data(BNO055_ADDRESS, REG_EUL_HEADING, 2)
                    break
                except OSError:
                    time.sleep(0.01 * (attempt + 1))
            if data is None:
                time.sleep(0.02)
                continue

            raw = data[0] | (data[1] << 8)
            if raw >= 0x8000:
                raw -= 0x10000
            heading = (raw / 16.0 + HEADING_OFFSET) % 360

            if heading == 0.0:
                zero_count += 1
                if zero_count >= 10:
                    print("BNO055 crash, reinit...", flush=True)
                    imu_init(HEADING_OFFSET)
                    zero_count = 0
                    continue
            else:
                zero_count = 0

            with heading_lock:
                current_heading = heading
        except Exception as e:
            print(f"Heading error: {e}", flush=True)
        time.sleep(0.02)  # ~50Hz


# ── WiFi capture ──────────────────────────────────────────────────

def setup_monitor(iface):
    for cmd in [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"WARN: {' '.join(cmd)}: {r.stderr.strip()}", flush=True)
    r = subprocess.run(["iw", "dev", iface, "info"], capture_output=True, text=True)
    if "type monitor" in r.stdout:
        print(f"{iface} in monitor mode", flush=True)
        return True
    print(f"Failed to set monitor mode on {iface}", flush=True)
    return False


CHANNELS_24 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
CHANNELS_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165]
HOP_DWELL = 0.15  # seconds per channel — fast hopping


def channel_hop_loop(iface):
    """Hop through all WiFi channels quickly."""
    channels = CHANNELS_24 + CHANNELS_5
    while True:
        for ch in channels:
            subprocess.run(
                ["iw", "dev", iface, "set", "channel", str(ch)],
                capture_output=True, text=True,
            )
            time.sleep(HOP_DWELL)


def capture_loop(iface):
    """Run tcpdump, parse each packet, pair with current heading, enqueue."""
    cmd = [
        "tcpdump", "-i", iface, "-e", "-l", "--immediate-mode", "-n",
        "type", "mgt", "subtype", "beacon", "or",
        "type", "mgt", "subtype", "probe-resp", "or",
        "type", "mgt", "subtype", "probe-req",
    ]
    print(f"Starting: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1)

    for line in proc.stdout:
        rssi_match = re.search(r'(-\d+)dBm signal', line)
        if not rssi_match:
            continue
        rssi = int(rssi_match.group(1))

        mac = None
        ssid = ""

        # Beacon or Probe Response -> AP
        ap_match = re.search(r'(Beacon|Probe Response) \(([^)]*)\)', line)
        if ap_match:
            ssid = ap_match.group(2)
            bssid_match = re.search(r'BSSID:(\S+)', line)
            if not bssid_match:
                bssid_match = re.search(r'SA:(\S+)', line)
            if bssid_match:
                mac = bssid_match.group(1).lower().rstrip(',')
        else:
            # Probe Request -> client device
            probe_match = re.search(r'Probe Request \(([^)]*)\)', line)
            if probe_match:
                ssid = probe_match.group(1)
                sa_match = re.search(r'SA:(\S+)', line)
                if sa_match:
                    mac = sa_match.group(1).lower().rstrip(',')

        if mac is None:
            continue

        ch_match = re.search(r'CH: (\d+)', line)
        channel = int(ch_match.group(1)) if ch_match else 0

        with heading_lock:
            h = current_heading

        pkt = {
            "h": round(h, 1),
            "b": mac,
            "s": ssid,
            "r": rssi,
            "c": channel,
        }

        try:
            packet_queue.put_nowait(pkt)
        except queue.Full:
            # Drop oldest
            try:
                packet_queue.get_nowait()
            except queue.Empty:
                pass
            packet_queue.put_nowait(pkt)


# ── WebSocket server ──────────────────────────────────────────────

connected_clients = set()


async def ws_handler(websocket):
    addr = websocket.remote_address
    print(f"Client connected from {addr}", flush=True)
    connected_clients.add(websocket)
    try:
        # Send dish identity
        await websocket.send(json.dumps({"type": "hello", "dish": DISH_ID}))
        # Keep connection alive, actual data sent by broadcast_loop
        async for msg in websocket:
            pass  # client doesn't send us anything
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        print(f"Client disconnected from {addr}", flush=True)


async def broadcast_loop():
    """Drain packet queue and broadcast to all connected clients."""
    loop = asyncio.get_event_loop()
    while True:
        # Drain queue in batches for efficiency
        batch = []
        try:
            while len(batch) < 100:
                pkt = packet_queue.get_nowait()
                batch.append(pkt)
        except queue.Empty:
            pass

        if batch and connected_clients:
            msg = json.dumps({"type": "packets", "dish": DISH_ID, "pkts": batch})
            dead = set()
            for ws in connected_clients:
                try:
                    await ws.send(msg)
                except Exception:
                    dead.add(ws)
            connected_clients.difference_update(dead)

        await asyncio.sleep(0.05)  # 20Hz broadcast rate


async def main():
    global DISH_ID, PORT, HEADING_OFFSET, WIFI_IFACE, CHANNEL

    if len(sys.argv) < 2:
        print(f"Usage: sudo python3 {sys.argv[0]} <dish_id> [port] [heading_offset] [wifi_iface] [channel]")
        sys.exit(1)

    DISH_ID = sys.argv[1]
    PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8082
    offset = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    WIFI_IFACE = sys.argv[4] if len(sys.argv) > 4 else "wlan1"
    CHANNEL = sys.argv[5] if len(sys.argv) > 5 else "6"

    if os.geteuid() != 0:
        print("ERROR: must run as root")
        sys.exit(1)

    # Init BNO055
    imu_init(offset)

    # Start heading reader thread
    threading.Thread(target=heading_loop, daemon=True).start()

    # Setup WiFi monitor mode
    if not setup_monitor(WIFI_IFACE):
        sys.exit(1)

    # Always hop channels for maximum coverage
    threading.Thread(target=channel_hop_loop, args=(WIFI_IFACE,), daemon=True).start()
    print(f"Channel hopping started (150ms dwell, 2.4+5GHz)", flush=True)

    # Start WiFi capture thread
    threading.Thread(target=capture_loop, args=(WIFI_IFACE,), daemon=True).start()

    print(f"Dish streamer started: {DISH_ID} on :{PORT}", flush=True)
    print(f"  Streaming raw (heading, bssid, ssid, rssi, channel) per packet", flush=True)

    # Start WebSocket server + broadcast loop
    async with serve(ws_handler, "0.0.0.0", PORT):
        await broadcast_loop()


if __name__ == "__main__":
    asyncio.run(main())
