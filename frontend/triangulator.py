#!/usr/bin/env python3
"""WiFi triangulation engine.

Connects to WebSocket servers on each Pi (dish_client.py on :8082)
to receive heading + RSSI data. Computes peak bearings per target,
triangulates positions, and pushes results to wb.py.

Algorithm ported from wifi-densepose/radar.py:
- 5-degree bins, 72 sectors for 360 degrees
- Accumulates samples across many sweeps (10-minute window)
- Average RSSI per bin (not max) for noise reduction
- Weighted circular mean for bearing estimation
- Confidence scoring: dB swing / 15
- Floor filtering: drop signals below -88 dBm

Runs on the local machine.

Usage:
    python3 triangulator.py [dish1_ip] [dish2_ip]
"""

import asyncio
import json
import logging
import math
import sys
import time
from collections import defaultdict

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("triangulator")

DISH_1_IP = "192.168.1.101"
DISH_2_IP = "192.168.1.103"
DISH_WS_PORT = 8082

COMPUTE_INTERVAL = 1.0

dish_config = {
    "distance": 3.82,
    "bearing": 94.0,
}

REF_LAT = 37.7725
REF_LON = -122.415

# Signal processing constants (from wifi-densepose/radar.py)
FLOOR_SIGNAL = -88       # dBm — reject signals weaker than this
EMA_ALPHA = 0.3          # smoothing factor for RSSI
HISTORY_WINDOW = 600     # seconds (10 minutes) of sample accumulation
MIN_FRAMES = 1           # minimum packets to consider a target
MIN_CONFIDENCE = 0.05    # minimum confidence to use a bearing
MAX_RANGE = 200          # meters — reject positions further than this

# Bearing estimation
NUM_BINS = 72            # 5-degree bins
BIN_WIDTH = 5.0
MIN_BINS_COVERED = 4     # need at least 20 degrees of coverage (builds over time)

# RSSI distance model
TX_POWER = -30
PATH_LOSS_N = 2.7


def dish2_position_meters():
    d = dish_config["distance"]
    b = math.radians(dish_config["bearing"])
    return (d * math.sin(b), d * math.cos(b))


def meters_to_latlon(x, y):
    lat = REF_LAT + (y / 111320.0)
    lon = REF_LON + (x / (111320.0 * math.cos(math.radians(REF_LAT))))
    return (lat, lon)


def rssi_to_distance(rssi):
    if rssi >= TX_POWER:
        return 1.0
    return 10 ** ((TX_POWER - rssi) / (10 * PATH_LOSS_N))


def estimate_bearing(history):
    """Estimate bearing from accumulated (heading, rssi) samples.

    Ported from wifi-densepose/radar.py estimate_bearing().
    Uses 5-degree bins with average RSSI per bin, then weighted
    circular mean of all bins (weight = max(0, avg_signal - floor)).

    Returns (bearing_deg, confidence, peak_signal) or (None, 0, None).
    """
    if not history:
        return None, 0, None

    # Bin into 5-degree buckets, average signal per bucket
    bins = defaultdict(list)
    for heading, rssi in history:
        b = int(heading / BIN_WIDTH) % NUM_BINS
        bins[b].append(rssi)

    profile = {}
    for b, sigs in bins.items():
        profile[b] = sum(sigs) / len(sigs)

    if len(profile) < MIN_BINS_COVERED:
        return None, 0, None

    peak_bin = max(profile, key=profile.get)
    peak_signal = profile[peak_bin]
    min_signal = min(profile.values())

    # Weighted circular mean — weight = signal above floor
    sin_sum = cos_sum = weight_sum = 0.0
    for b, avg_sig in profile.items():
        w = max(0.0, avg_sig - FLOOR_SIGNAL)
        angle_rad = math.radians(b * BIN_WIDTH)
        sin_sum += w * math.sin(angle_rad)
        cos_sum += w * math.cos(angle_rad)
        weight_sum += w

    if weight_sum == 0:
        return peak_bin * BIN_WIDTH, 0, peak_signal

    bearing = math.degrees(math.atan2(sin_sum, cos_sum)) % 360

    # Confidence: peak-to-min contrast (wall behind dish creates bimodal pattern)
    # 10 dB contrast between front and back = full confidence
    swing = peak_signal - min_signal
    confidence = min(1.0, max(0.0, swing / 10.0))

    return round(bearing, 1), round(confidence, 3), peak_signal


def intersect_bearings(p1, b1, p2, b2):
    """Find intersection of two bearing lines.

    Ported from wifi-densepose/triangulate.py.
    Converts compass bearings to math angles for intersection.
    """
    # Convert compass bearing (0=N, 90=E) to math angle (CCW from east)
    a1 = math.radians(90 - b1)
    a2 = math.radians(90 - b2)

    dx1, dy1 = math.cos(a1), math.sin(a1)
    dx2, dy2 = math.cos(a2), math.sin(a2)

    denom = dx1 * dy2 - dy1 * dx2
    if abs(denom) < 0.05:
        return None  # nearly parallel — unreliable

    t1 = ((p2[0] - p1[0]) * dy2 - (p2[1] - p1[1]) * dx2) / denom

    x = p1[0] + t1 * dx1
    y = p1[1] + t1 * dy1
    return (round(x, 2), round(y, 2))


class TriangulationEngine:
    def __init__(self):
        # dish_id -> {bssid: {signal, frames, history, last_seen}}
        self.devices = {}
        self.ssid_map = {}
        self._last_prune = time.time()

    def ingest_packet(self, dish_id, heading, bssid, ssid, rssi):
        """Ingest a single raw packet: one beacon/probe with its heading."""
        # Floor filter
        if rssi < FLOOR_SIGNAL:
            return

        if dish_id not in self.devices:
            self.devices[dish_id] = {}
        devs = self.devices[dish_id]
        now = time.time()

        if ssid:
            self.ssid_map[bssid] = ssid

        if bssid not in devs:
            devs[bssid] = {
                "signal": rssi,
                "frames": 0,
                "history": [],
                "last_seen": now,
            }

        d = devs[bssid]
        d["frames"] += 1
        d["last_seen"] = now
        d["signal"] = int(EMA_ALPHA * rssi + (1 - EMA_ALPHA) * d["signal"])
        d["history"].append((round(heading, 1), rssi))
        # Cap memory — keep last 5000 raw packets per target (~10 min)
        if len(d["history"]) > 5000:
            d["history"] = d["history"][-5000:]

        # Periodic prune of stale devices
        if now - self._last_prune > 30:
            self._last_prune = now
            for did in list(self.devices.keys()):
                for b in list(self.devices[did].keys()):
                    if now - self.devices[did][b]["last_seen"] > 120:
                        del self.devices[did][b]

    def compute(self):
        """Compute positions for all targets."""
        dish_ids = sorted(self.devices.keys())
        if not dish_ids:
            return []

        p1 = (0.0, 0.0)
        p2 = dish2_position_meters()
        dish_pos = {}
        if len(dish_ids) >= 1:
            dish_pos[dish_ids[0]] = p1
        if len(dish_ids) >= 2:
            dish_pos[dish_ids[1]] = p2

        # Compute bearings per dish per target
        dish_bearings = {}  # dish_id -> {bssid: (bearing, confidence, signal)}
        for did in dish_ids:
            dish_bearings[did] = {}
            for bssid, d in self.devices[did].items():
                if d["frames"] < MIN_FRAMES:
                    continue
                bearing, conf, peak = estimate_bearing(d["history"])
                if bearing is not None and conf >= MIN_CONFIDENCE:
                    dish_bearings[did][bssid] = (bearing, conf, d["signal"])

        # Collect all targets seen by any dish with a valid bearing
        all_targets = set()
        for did in dish_ids:
            all_targets.update(dish_bearings[did].keys())

        results = []
        for bssid in all_targets:
            bearings = {}
            confidences = {}
            best_rssi = -100

            for did in dish_ids:
                if bssid in dish_bearings[did]:
                    b, c, sig = dish_bearings[did][bssid]
                    bearings[did] = b
                    confidences[did] = c
                    best_rssi = max(best_rssi, sig)

            lat, lon = None, None
            fix = False

            # Two-dish triangulation
            if len(bearings) >= 2 and len(dish_ids) >= 2:
                d1, d2 = dish_ids[0], dish_ids[1]
                if d1 in bearings and d2 in bearings:
                    pos = intersect_bearings(
                        dish_pos[d1], bearings[d1],
                        dish_pos[d2], bearings[d2])
                    if pos is not None:
                        dist = math.sqrt(pos[0]**2 + pos[1]**2)
                        if dist <= MAX_RANGE:
                            lat, lon = meters_to_latlon(pos[0], pos[1])
                            fix = True

            # Single-dish fallback
            if not fix and bearings:
                did = next(iter(bearings))
                dist = rssi_to_distance(best_rssi)
                origin = dish_pos.get(did, p1)
                b_rad = math.radians(bearings[did])
                x = origin[0] + dist * math.sin(b_rad)
                y = origin[1] + dist * math.cos(b_rad)
                d_from_origin = math.sqrt(x**2 + y**2)
                if d_from_origin <= MAX_RANGE:
                    lat, lon = meters_to_latlon(x, y)

            if lat is not None:
                conf = min(confidences.values()) if confidences else 0
                results.append({
                    "mac": bssid,
                    "name": self.ssid_map.get(bssid, bssid),
                    "rssi": best_rssi,
                    "fix": fix,
                    "confidence": round(conf, 2),
                    "lat": round(lat, 7),
                    "lon": round(lon, 7),
                })

        return results


engine = TriangulationEngine()


async def listen_dish(ip, dish_id):
    """Connect to a Pi's dish_client WebSocket server and ingest raw packets."""
    url = f"ws://{ip}:{DISH_WS_PORT}"
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("%s connected at %s", dish_id, url)
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "packets":
                            for pkt in data.get("pkts", []):
                                heading = pkt.get("h")
                                rssi = pkt.get("r")
                                bssid = pkt.get("b")
                                if heading is None or rssi is None or bssid is None:
                                    continue
                                ssid = pkt.get("s", "")
                                engine.ingest_packet(dish_id, heading, bssid, ssid, rssi)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as e:
            log.warning("%s connection failed: %s, retrying in 2s...", dish_id, e)
            await asyncio.sleep(2)


async def push_loop():
    """Periodically compute positions and push to wb.py."""
    ws_conns = {}

    async def get_ws(key, url, register_msg=None):
        ws = ws_conns.get(key)
        if ws and ws.state.name == "OPEN":
            return ws
        try:
            ws = await websockets.connect(url)
            if register_msg:
                await ws.send(json.dumps(register_msg))
                await ws.recv()
            ws_conns[key] = ws
            return ws
        except Exception:
            ws_conns.pop(key, None)
            return None

    while True:
        await asyncio.sleep(COMPUTE_INTERVAL)

        devices = engine.compute()

        # Push device positions to :5003
        if devices:
            ws = await get_ws("device", "ws://localhost:5003")
            if ws:
                try:
                    await ws.send(json.dumps({"devices": devices}))
                except Exception:
                    ws_conns.pop("device", None)

        # Push dish headings to :5004
        for dish_id in ["DISH-1", "DISH-2"]:
            dish_devs = engine.devices.get(dish_id, {})
            latest_heading = None
            latest_t = 0
            for d in dish_devs.values():
                if d["history"] and d["last_seen"] > latest_t:
                    latest_t = d["last_seen"]
                    latest_heading = d["history"][-1][0]
            if latest_heading is not None:
                ws = await get_ws(dish_id, "ws://localhost:5004",
                                  {"data": "connect", "id": dish_id})
                if ws:
                    try:
                        await ws.send(json.dumps({"degrees": latest_heading}))
                    except Exception:
                        ws_conns.pop(dish_id, None)

        fixes = sum(1 for d in devices if d["fix"]) if devices else 0
        if devices:
            log.info("%d targets, %d fixed", len(devices), fixes)


async def config_listener():
    """Listen for config from wb.py :5005."""
    while True:
        try:
            async with websockets.connect("ws://localhost:5005") as ws:
                await ws.send(json.dumps({"data": "connect", "id": f"TRI-{int(time.time())}"}))
                log.info("Config listener connected to :5005")
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "config":
                            if "distance" in data:
                                dish_config["distance"] = float(data["distance"])
                            if "bearing" in data:
                                dish_config["bearing"] = float(data["bearing"])
                            log.info("Config: dist=%.1fm bearing=%.1f deg",
                                     dish_config["distance"], dish_config["bearing"])
                    except (json.JSONDecodeError, ValueError):
                        pass
        except Exception:
            await asyncio.sleep(2)


async def main():
    dish1_ip = sys.argv[1] if len(sys.argv) > 1 else DISH_1_IP
    dish2_ip = sys.argv[2] if len(sys.argv) > 2 else DISH_2_IP

    log.info("Triangulator starting")
    log.info("  DISH-1: ws://%s:%d", dish1_ip, DISH_WS_PORT)
    log.info("  DISH-2: ws://%s:%d", dish2_ip, DISH_WS_PORT)

    await asyncio.gather(
        listen_dish(dish1_ip, "DISH-1"),
        listen_dish(dish2_ip, "DISH-2"),
        push_loop(),
        config_listener(),
    )


if __name__ == "__main__":
    asyncio.run(main())
