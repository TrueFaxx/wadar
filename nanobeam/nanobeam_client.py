#!/usr/bin/env python3
"""NanoBeam AC Gen2 dish client.

Replaces pi_code/dish_client.py for Ubiquiti NanoBeam AC Gen2 hardware.
Instead of a Raspberry Pi with BNO055 IMU + monitor-mode WiFi, this
connects to a NanoBeam via SSH and polls for visible stations.

The NanoBeam has a fixed directional antenna (~15 degree beamwidth at 5GHz),
so heading is static (set at deploy time based on physical aim).

Data sources:
  - Station list via SSH `wstalist` (AirOS command)
  - Scan results via SSH `iwlist ath0 scan` for non-associated devices
  - Heading is fixed (configured at startup from physical mount angle)

Streams packets to the WADAR triangulator via WebSocket, using the same
protocol as dish_client.py.

Usage:
    python3 nanobeam_client.py <dish_id> <nanobeam_ip> <heading_deg> [port] [user] [pass]

Example:
    python3 nanobeam_client.py DISH-1 192.168.1.20 45.0 8082 ubnt ubnt
"""

import asyncio
import json
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
NANOBEAM_IP = "192.168.1.20"
PORT = 8082
FIXED_HEADING = 0.0  # degrees, set from physical mount angle
NB_USER = "ubnt"
NB_PASS = "ubnt"

POLL_INTERVAL = 1.0  # seconds between station polls
SCAN_INTERVAL = 10.0  # seconds between full WiFi scans

# Queue of packets to stream
packet_queue = queue.Queue(maxsize=5000)

# ── SSH helpers ──────────────────────────────────────────────────

def ssh_cmd(ip, user, password, cmd, timeout=10):
    """Run a command on the NanoBeam via SSH, return stdout."""
    ssh = [
        "sshpass", "-p", password,
        "ssh",
        "-o", "PubkeyAuthentication=no",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={timeout}",
        "-o", "ServerAliveInterval=5",
        user + "@" + ip,
        cmd,
    ]
    try:
        r = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout + 5)
        return r.stdout
    except (subprocess.TimeoutExpired, Exception) as e:
        print(f"SSH error ({ip}): {e}", flush=True)
        return ""


def parse_wstalist(output):
    """Parse AirOS `wstalist` JSON output.

    Returns list of dicts with keys: mac, name, rssi, signal, noise, ccq, tx, rx
    """
    try:
        stations = json.loads(output)
        results = []
        for s in stations:
            results.append({
                "mac": s.get("mac", "").lower(),
                "name": s.get("name", s.get("hostname", "")),
                "rssi": s.get("signal", s.get("rssi", -90)),
                "lastip": s.get("lastip", ""),
                "ccq": s.get("ccq", 0),
            })
        return results
    except (json.JSONDecodeError, TypeError):
        return []


def parse_mca_status(output):
    """Parse AirOS `mca-status` output for device info and connected stations."""
    info = {}
    for line in output.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip()
    return info


def parse_iwlist_scan(output):
    """Parse `iwlist ath0 scan` output for nearby networks.

    Returns list of dicts: mac, ssid, rssi, channel
    """
    results = []
    current = None

    for line in output.splitlines():
        line = line.strip()

        cell_match = re.match(r'Cell \d+ - Address: ([\dA-Fa-f:]+)', line)
        if cell_match:
            if current and current.get("mac"):
                results.append(current)
            current = {"mac": cell_match.group(1).lower(), "ssid": "", "rssi": -90, "channel": 0}
            continue

        if current is None:
            continue

        if line.startswith("ESSID:"):
            m = re.search(r'ESSID:"([^"]*)"', line)
            if m:
                current["ssid"] = m.group(1)
        elif "Signal level" in line:
            m = re.search(r'Signal level[=:]?\s*(-?\d+)', line)
            if m:
                current["rssi"] = int(m.group(1))
        elif line.startswith("Channel:"):
            m = re.search(r'Channel:\s*(\d+)', line)
            if m:
                current["channel"] = int(m.group(1))

    if current and current.get("mac"):
        results.append(current)

    return results


# ── Polling loops ────────────────────────────────────────────────

def station_poll_loop(ip, user, password, heading):
    """Poll associated stations via wstalist at regular intervals."""
    while True:
        try:
            output = ssh_cmd(ip, user, password, "wstalist")
            stations = parse_wstalist(output)
            for s in stations:
                if not s["mac"]:
                    continue
                pkt = {
                    "h": heading,
                    "b": s["mac"],
                    "s": s["name"],
                    "r": s["rssi"],
                    "c": 0,
                }
                try:
                    packet_queue.put_nowait(pkt)
                except queue.Full:
                    try:
                        packet_queue.get_nowait()
                    except queue.Empty:
                        pass
                    packet_queue.put_nowait(pkt)

            if stations:
                print(f"[{DISH_ID}] {len(stations)} associated stations", flush=True)
        except Exception as e:
            print(f"Station poll error: {e}", flush=True)
        time.sleep(POLL_INTERVAL)


def scan_poll_loop(ip, user, password, heading):
    """Run periodic WiFi scans for non-associated devices."""
    while True:
        try:
            # Try ath0 first (AirOS), fallback to wlan0
            output = ssh_cmd(ip, user, password, "iwlist ath0 scan 2>/dev/null || iwlist wlan0 scan 2>/dev/null", timeout=15)
            devices = parse_iwlist_scan(output)
            for d in devices:
                if not d["mac"]:
                    continue
                pkt = {
                    "h": heading,
                    "b": d["mac"],
                    "s": d["ssid"],
                    "r": d["rssi"],
                    "c": d.get("channel", 0),
                }
                try:
                    packet_queue.put_nowait(pkt)
                except queue.Full:
                    try:
                        packet_queue.get_nowait()
                    except queue.Empty:
                        pass
                    packet_queue.put_nowait(pkt)

            if devices:
                print(f"[{DISH_ID}] Scan found {len(devices)} nearby networks", flush=True)
        except Exception as e:
            print(f"Scan error: {e}", flush=True)
        time.sleep(SCAN_INTERVAL)


def heading_push_loop(heading):
    """Periodically push fixed heading updates (mimics BNO055 heading stream)."""
    while True:
        pkt = {"h": heading, "t": "heading", "cal": {"sys": 3, "gyro": 3, "accel": 3, "mag": 3}}
        try:
            packet_queue.put_nowait(pkt)
        except queue.Full:
            pass
        time.sleep(5.0)


# ── WebSocket server ──────────────────────────────────────────────

connected_clients = set()


async def ws_handler(websocket):
    addr = websocket.remote_address
    print(f"Client connected from {addr}", flush=True)
    connected_clients.add(websocket)
    try:
        await websocket.send(json.dumps({"type": "hello", "dish": DISH_ID}))
        async for msg in websocket:
            try:
                data = json.loads(msg)
                if data.get("cmd") == "set_channel":
                    ch = data.get("channel")
                    print(f"Channel command received: {ch} (NanoBeam uses fixed channel)", flush=True)
            except (json.JSONDecodeError, ValueError):
                pass
    except websockets.ConnectionClosed:
        pass
    finally:
        connected_clients.discard(websocket)
        print(f"Client disconnected from {addr}", flush=True)


async def broadcast_loop():
    """Drain packet queue and broadcast to all connected clients."""
    while True:
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
            for ws in list(connected_clients):
                try:
                    await ws.send(msg)
                except Exception:
                    dead.add(ws)
            connected_clients.difference_update(dead)

        await asyncio.sleep(0.1)


# ── Main ──────────────────────────────────────────────────────────

async def main():
    global DISH_ID, NANOBEAM_IP, FIXED_HEADING, PORT, NB_USER, NB_PASS

    if len(sys.argv) < 4:
        print(f"Usage: python3 {sys.argv[0]} <dish_id> <nanobeam_ip> <heading_deg> [port] [user] [pass]")
        print()
        print("  dish_id      : DISH-1 or DISH-2")
        print("  nanobeam_ip  : IP address of the NanoBeam AC Gen2")
        print("  heading_deg  : Fixed compass heading the NanoBeam points (0-360)")
        print("  port         : WebSocket server port (default: 8082)")
        print("  user         : SSH username (default: ubnt)")
        print("  pass         : SSH password (default: ubnt)")
        sys.exit(1)

    DISH_ID = sys.argv[1]
    NANOBEAM_IP = sys.argv[2]
    FIXED_HEADING = float(sys.argv[3])
    PORT = int(sys.argv[4]) if len(sys.argv) > 4 else 8082
    NB_USER = sys.argv[5] if len(sys.argv) > 5 else "ubnt"
    NB_PASS = sys.argv[6] if len(sys.argv) > 6 else "ubnt"

    # Verify SSH connectivity
    print(f"Testing SSH to {NANOBEAM_IP}...", flush=True)
    test = ssh_cmd(NANOBEAM_IP, NB_USER, NB_PASS, "cat /etc/version")
    if test.strip():
        print(f"  AirOS version: {test.strip()}", flush=True)
    else:
        print(f"  WARNING: SSH test failed — check IP/credentials", flush=True)
        print(f"  Will keep retrying in the background...", flush=True)

    # Start polling threads
    threading.Thread(target=station_poll_loop,
                     args=(NANOBEAM_IP, NB_USER, NB_PASS, FIXED_HEADING),
                     daemon=True).start()
    threading.Thread(target=scan_poll_loop,
                     args=(NANOBEAM_IP, NB_USER, NB_PASS, FIXED_HEADING),
                     daemon=True).start()
    threading.Thread(target=heading_push_loop,
                     args=(FIXED_HEADING,),
                     daemon=True).start()

    print(f"NanoBeam dish client started: {DISH_ID}", flush=True)
    print(f"  NanoBeam IP:  {NANOBEAM_IP}", flush=True)
    print(f"  Heading:      {FIXED_HEADING} deg (fixed)", flush=True)
    print(f"  WS port:      {PORT}", flush=True)
    print(f"  Polling:      stations every {POLL_INTERVAL}s, scan every {SCAN_INTERVAL}s", flush=True)

    async with serve(ws_handler, "0.0.0.0", PORT):
        await broadcast_loop()


if __name__ == "__main__":
    asyncio.run(main())
