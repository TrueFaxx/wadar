#!/usr/bin/env python3
"""WiFi monitor mode capture server.

Puts a WiFi adapter into monitor mode, hops channels to capture
beacon/probe frames via tcpdump, and serves RSSI + AP/device data
as JSON over HTTP (same pattern as sensor_server.py).

Captures:
  - APs: beacons and probe responses (BSSID, SSID, RSSI, channel)
  - Devices: probe requests from clients (MAC, RSSI, probed SSIDs)

Usage:
    sudo python3 monitor_capture.py [interface] [port] [channel]

    interface  - WiFi interface to use (default: wlan1)
    port       - HTTP server port (default: 8081)
    channel    - WiFi channel to monitor (default: 6, use "hop" for all)

Requires: tcpdump, iw, ip
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# 2.4GHz channels (1-11) + 5GHz common channels
CHANNELS_24 = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
CHANNELS_5 = [36, 40, 44, 48, 149, 153, 157, 161, 165]
HOP_DWELL = 0.5  # seconds per channel

# Shared state
lock = threading.Lock()
ap_table = {}       # BSSID -> {ssid, rssi, channel, last_seen, ...}
device_table = {}   # MAC -> {rssi, last_seen, probed_ssids, count, ...}
capture_proc = None
iface = "wlan1"
current_channel = 0


def setup_monitor(interface):
    """Put interface into monitor mode."""
    cmds = [
        ["ip", "link", "set", interface, "down"],
        ["iw", "dev", interface, "set", "type", "monitor"],
        ["ip", "link", "set", interface, "up"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"WARN: {' '.join(cmd)} failed: {r.stderr.strip()}")
            return False
    r = subprocess.run(["iw", "dev", interface, "info"],
                       capture_output=True, text=True)
    if "type monitor" in r.stdout:
        print(f"{interface} is in monitor mode")
        return True
    print(f"Failed to set monitor mode on {interface}")
    return False


def channel_hop_loop(interface):
    """Hop through WiFi channels so we see all APs/devices."""
    global current_channel
    channels = CHANNELS_24 + CHANNELS_5
    while True:
        for ch in channels:
            r = subprocess.run(
                ["iw", "dev", interface, "set", "channel", str(ch)],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                current_channel = ch
            time.sleep(HOP_DWELL)


def parse_line(line):
    """Parse a tcpdump -e line for beacon/probe/device info."""
    rssi_match = re.search(r'(-\d+)dBm signal', line)
    if not rssi_match:
        return None
    rssi = int(rssi_match.group(1))

    # Beacon or Probe Response -> AP
    ap_match = re.search(r'(Beacon|Probe Response) \(([^)]*)\)', line)
    if ap_match:
        ssid = ap_match.group(2)
        bssid_match = re.search(r'BSSID:(\S+)', line)
        if not bssid_match:
            bssid_match = re.search(r'SA:(\S+)', line)
        if not bssid_match:
            return None
        bssid = bssid_match.group(1).lower().rstrip(',')
        ch_match = re.search(r'CH: (\d+)', line)
        channel = int(ch_match.group(1)) if ch_match else current_channel
        return {"type": "ap", "bssid": bssid, "ssid": ssid,
                "rssi": rssi, "channel": channel}

    # Probe Request -> device
    probe_match = re.search(r'Probe Request \(([^)]*)\)', line)
    if probe_match:
        ssid = probe_match.group(1)
        sa_match = re.search(r'SA:(\S+)', line)
        if not sa_match:
            return None
        mac = sa_match.group(1).lower().rstrip(',')
        return {"type": "device", "mac": mac, "ssid": ssid,
                "rssi": rssi, "channel": current_channel}

    return None


def capture_loop(interface):
    """Run tcpdump and parse output continuously."""
    global capture_proc
    cmd = [
        "tcpdump", "-i", interface, "-e", "-l", "--immediate-mode", "-n",
        "type", "mgt", "subtype", "beacon", "or",
        "type", "mgt", "subtype", "probe-resp", "or",
        "type", "mgt", "subtype", "probe-req",
    ]
    print(f"Starting: {' '.join(cmd)}")
    capture_proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    for line in capture_proc.stdout:
        parsed = parse_line(line)
        if not parsed:
            continue
        now = time.time()

        with lock:
            if parsed["type"] == "ap":
                bssid = parsed["bssid"]
                if bssid in ap_table:
                    entry = ap_table[bssid]
                    entry["rssi"] = parsed["rssi"]
                    entry["last_seen"] = now
                    entry["count"] += 1
                    if parsed["ssid"]:
                        entry["ssid"] = parsed["ssid"]
                    if parsed["channel"]:
                        entry["channel"] = parsed["channel"]
                    entry["rssi_history"].append(
                        {"rssi": parsed["rssi"], "t": now})
                    if len(entry["rssi_history"]) > 60:
                        entry["rssi_history"] = entry["rssi_history"][-60:]
                else:
                    ap_table[bssid] = {
                        "ssid": parsed["ssid"] or "",
                        "rssi": parsed["rssi"],
                        "channel": parsed["channel"],
                        "first_seen": now, "last_seen": now,
                        "count": 1,
                        "rssi_history": [{"rssi": parsed["rssi"], "t": now}],
                    }

            elif parsed["type"] == "device":
                mac = parsed["mac"]
                if mac in device_table:
                    entry = device_table[mac]
                    entry["rssi"] = parsed["rssi"]
                    entry["last_seen"] = now
                    entry["count"] += 1
                    if parsed["ssid"] and parsed["ssid"] not in entry["probed_ssids"]:
                        entry["probed_ssids"].append(parsed["ssid"])
                    entry["rssi_history"].append(
                        {"rssi": parsed["rssi"], "t": now})
                    if len(entry["rssi_history"]) > 60:
                        entry["rssi_history"] = entry["rssi_history"][-60:]
                else:
                    device_table[mac] = {
                        "rssi": parsed["rssi"],
                        "probed_ssids": [parsed["ssid"]] if parsed["ssid"] else [],
                        "first_seen": now, "last_seen": now,
                        "count": 1,
                        "rssi_history": [{"rssi": parsed["rssi"], "t": now}],
                    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._serve_summary()
        elif self.path == "/devices":
            self._serve_devices()
        elif self.path.startswith("/ap/"):
            self._serve_ap_detail(self.path[4:])
        elif self.path.startswith("/device/"):
            self._serve_device_detail(self.path[8:])
        else:
            self.send_error(404)

    def _serve_summary(self):
        with lock:
            now = time.time()
            aps = []
            for bssid, info in ap_table.items():
                aps.append({
                    "bssid": bssid, "ssid": info["ssid"],
                    "rssi": info["rssi"], "channel": info["channel"],
                    "count": info["count"],
                    "age": round(now - info["last_seen"], 1),
                })
            aps.sort(key=lambda a: a["rssi"], reverse=True)
        self._json_response({
            "interface": iface, "channel": current_channel,
            "ap_count": len(aps), "device_count": len(device_table),
            "aps": aps, "time": time.time(),
        })

    def _serve_devices(self):
        with lock:
            now = time.time()
            devs = []
            for mac, info in device_table.items():
                devs.append({
                    "mac": mac, "rssi": info["rssi"],
                    "probed_ssids": info["probed_ssids"],
                    "count": info["count"],
                    "age": round(now - info["last_seen"], 1),
                })
            devs.sort(key=lambda d: d["rssi"], reverse=True)
        self._json_response({
            "interface": iface, "device_count": len(devs),
            "devices": devs, "time": time.time(),
        })

    def _serve_ap_detail(self, bssid):
        bssid = bssid.lower()
        with lock:
            if bssid not in ap_table:
                self.send_error(404)
                return
            info = ap_table[bssid].copy()
            info["rssi_history"] = list(ap_table[bssid]["rssi_history"])
        self._json_response({
            "bssid": bssid, "ssid": info["ssid"],
            "rssi": info["rssi"], "channel": info["channel"],
            "count": info["count"],
            "rssi_history": info["rssi_history"], "time": time.time(),
        })

    def _serve_device_detail(self, mac):
        mac = mac.lower()
        with lock:
            if mac not in device_table:
                self.send_error(404)
                return
            info = device_table[mac].copy()
            info["rssi_history"] = list(device_table[mac]["rssi_history"])
        self._json_response({
            "mac": mac, "rssi": info["rssi"],
            "probed_ssids": info["probed_ssids"],
            "count": info["count"],
            "rssi_history": info["rssi_history"], "time": time.time(),
        })

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def cleanup(signum, frame):
    if capture_proc and capture_proc.poll() is None:
        capture_proc.terminate()
    sys.exit(0)


def main():
    global iface, current_channel

    iface = sys.argv[1] if len(sys.argv) > 1 else "wlan1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8081
    chan_arg = sys.argv[3] if len(sys.argv) > 3 else "6"

    if os.geteuid() != 0:
        print("ERROR: Must run as root (need monitor mode + tcpdump)")
        sys.exit(1)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    print(f"Setting up monitor mode on {iface}...", flush=True)
    if not setup_monitor(iface):
        sys.exit(1)

    if chan_arg == "hop":
        threading.Thread(target=channel_hop_loop, args=(iface,), daemon=True).start()
        print("Channel hopping started (WARNING: may disrupt wlan0)", flush=True)
    else:
        ch = int(chan_arg)
        subprocess.run(["iw", "dev", iface, "set", "channel", str(ch)],
                       capture_output=True, text=True)
        current_channel = ch
        print(f"Locked to channel {ch}", flush=True)

    # Start capture
    threading.Thread(target=capture_loop, args=(iface,), daemon=True).start()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving on port {port}", flush=True)
    print(f"  GET /           - APs summary", flush=True)
    print(f"  GET /devices    - client devices", flush=True)
    print(f"  GET /ap/BSSID   - AP RSSI history", flush=True)
    print(f"  GET /device/MAC - device RSSI history", flush=True)
    try:
        server.serve_forever()
    finally:
        if capture_proc and capture_proc.poll() is None:
            capture_proc.terminate()
        server.server_close()


if __name__ == "__main__":
    main()
