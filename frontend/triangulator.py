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
import uuid
from collections import defaultdict

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("triangulator")

DISH_1_IP = "192.168.1.103"  # Pi 3
DISH_2_IP = "192.168.1.101"  # Pi 4
DISH_WS_PORT = 8082

COMPUTE_INTERVAL = 0.25  # 4Hz compute + heading push

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
MIN_FRAMES = 5           # minimum packets to consider a target (filters drive-by probes)
MIN_CONFIDENCE = 0.05    # minimum confidence to use a bearing
MAX_RANGE = 200          # meters — reject positions further than this

# Bearing estimation
NUM_BINS = 72            # 5-degree bins
BIN_WIDTH = 5.0
MIN_BINS_COVERED = 2     # minimum bins needed (more bins = more accurate bearing)

# RSSI distance model


def dish2_position_meters():
    d = dish_config["distance"]
    b = math.radians(dish_config["bearing"])
    return (d * math.sin(b), d * math.cos(b))


def meters_to_latlon(x, y):
    lat = REF_LAT + (y / 111320.0)
    lon = REF_LON + (x / (111320.0 * math.cos(math.radians(REF_LAT))))
    return (lat, lon)


def estimate_bearing(history):
    """Estimate bearing from accumulated (heading, rssi) samples.

    Bins into 5-degree sectors, finds the peak bin (strongest average RSSI),
    then refines the bearing using a weighted average of the peak and its
    immediate neighbors. This finds the actual direction of maximum signal,
    not the center-of-mass of all signal.

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

    # Refine bearing using peak bin + neighbors (parabolic interpolation)
    # Use up to 2 bins on each side of the peak
    neighbors = []
    for offset in range(-2, 3):
        nb = (peak_bin + offset) % NUM_BINS
        if nb in profile:
            neighbors.append((nb, profile[nb]))

    # Weighted circular mean of ONLY the peak neighborhood
    sin_sum = cos_sum = weight_sum = 0.0
    for b, avg_sig in neighbors:
        # Weight by how much stronger than the minimum (emphasize the peak)
        w = max(0.0, avg_sig - min_signal)
        angle_rad = math.radians((b + 0.5) * BIN_WIDTH)
        sin_sum += w * math.sin(angle_rad)
        cos_sum += w * math.cos(angle_rad)
        weight_sum += w

    if weight_sum == 0:
        bearing = (peak_bin + 0.5) * BIN_WIDTH
    else:
        bearing = math.degrees(math.atan2(sin_sum, cos_sum)) % 360

    # Confidence: peak-to-min contrast
    # 10 dB contrast between strongest and weakest direction = full confidence
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
        # UUID tracking: bssid -> stable UUID
        self.uuid_map = {}
        # Path tracking: bssid -> [(lat, lon, timestamp), ...]
        self.path_history = {}
        self.PATH_MAX_POINTS = 500
        self.PATH_MIN_INTERVAL = 2.0  # seconds between path points

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

        # Assign stable UUID on first sight
        if bssid not in self.uuid_map:
            self.uuid_map[bssid] = str(uuid.uuid4())

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

        # Collect ALL targets seen by any dish
        all_targets = set()
        for did in dish_ids:
            all_targets.update(self.devices[did].keys())

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
                if bssid in self.devices.get(did, {}):
                    best_rssi = max(best_rssi, self.devices[did][bssid]["signal"])

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

            # Bearing and distance from dishes (only if we have a fix)
            bearing_from_d1 = bearings.get(dish_ids[0]) if dish_ids else None
            bearing_from_d2 = bearings.get(dish_ids[1]) if len(dish_ids) >= 2 else None
            dist_from_d1 = None
            dist_from_d2 = None

            if fix:
                dx_m = (lon - REF_LON) * 111320.0 * math.cos(math.radians(REF_LAT))
                dy_m = (lat - REF_LAT) * 111320.0
                dist_from_d1 = round(math.sqrt(dx_m**2 + dy_m**2), 1)
                if bearing_from_d1 is None:
                    bearing_from_d1 = round(math.degrees(math.atan2(dx_m, dy_m)) % 360, 1)
                if len(dish_ids) >= 2:
                    p2 = dish2_position_meters()
                    p2_lat, p2_lon = meters_to_latlon(p2[0], p2[1])
                    dx2_m = (lon - p2_lon) * 111320.0 * math.cos(math.radians(REF_LAT))
                    dy2_m = (lat - p2_lat) * 111320.0
                    dist_from_d2 = round(math.sqrt(dx2_m**2 + dy2_m**2), 1)

            # Record path point if we have a fix
            now = time.time()
            if fix and lat and lon:
                if bssid not in self.path_history:
                    self.path_history[bssid] = []
                path = self.path_history[bssid]
                # Only add if enough time has elapsed since last point
                if not path or (now - path[-1][2]) >= self.PATH_MIN_INTERVAL:
                    path.append((round(lat, 7), round(lon, 7), round(now, 1)))
                    if len(path) > self.PATH_MAX_POINTS:
                        self.path_history[bssid] = path[-self.PATH_MAX_POINTS:]

            # Build path for output (lat/lon pairs with timestamps)
            device_path = []
            if bssid in self.path_history:
                device_path = [
                    {"lat": p[0], "lon": p[1], "ts": p[2]}
                    for p in self.path_history[bssid]
                ]

            conf = min(confidences.values()) if confidences else 0
            results.append({
                "mac": bssid,
                "uuid": self.uuid_map.get(bssid, ""),
                "name": self.ssid_map.get(bssid, bssid),
                "rssi": best_rssi,
                "fix": fix,
                "confidence": round(conf, 2),
                "lat": round(lat, 7) if lat else None,
                "lon": round(lon, 7) if lon else None,
                "bearing1": bearing_from_d1,
                "dist1": dist_from_d1,
                "bearing2": bearing_from_d2,
                "dist2": dist_from_d2,
                "path": device_path,
            })

        return results


engine = TriangulationEngine()


# Track latest heading per dish for the push_loop
dish_headings = {}
# Track calibration status per dish
dish_cal = {}  # dish_id -> {"sys": 0-3, "gyro": 0-3, "accel": 0-3, "mag": 0-3}
# Store Pi WebSocket connections so we can send commands (e.g. channel)
dish_ws = {}  # dish_id -> websocket


async def listen_dish(ip, dish_id):
    """Connect to a Pi's dish_client WebSocket server and ingest raw packets."""
    url = f"ws://{ip}:{DISH_WS_PORT}"
    while True:
        try:
            async with websockets.connect(url) as ws:
                log.info("%s connected at %s", dish_id, url)
                dish_ws[dish_id] = ws
                async for msg in ws:
                    try:
                        data = json.loads(msg)
                        if data.get("type") == "packets":
                            for pkt in data.get("pkts", []):
                                if pkt.get("t") == "heading":
                                    dish_headings[dish_id] = pkt["h"]
                                    if "cal" in pkt:
                                        dish_cal[dish_id] = pkt["cal"]
                                    continue
                                heading = pkt.get("h")
                                rssi = pkt.get("r")
                                bssid = pkt.get("b")
                                if heading is None or rssi is None or bssid is None:
                                    continue
                                dish_headings[dish_id] = heading
                                ssid = pkt.get("s", "")
                                engine.ingest_packet(dish_id, heading, bssid, ssid, rssi)
                    except (json.JSONDecodeError, KeyError):
                        pass
        except Exception as e:
            dish_ws.pop(dish_id, None)
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
            latest_heading = dish_headings.get(dish_id)
            if latest_heading is not None:
                ws = await get_ws(dish_id, "ws://localhost:5004",
                                  {"data": "connect", "id": dish_id})
                if ws:
                    try:
                        msg = {"degrees": latest_heading}
                        if dish_id in dish_cal:
                            msg["cal"] = dish_cal[dish_id]
                        await ws.send(json.dumps(msg))
                    except Exception:
                        ws_conns.pop(dish_id, None)

        fixes = sum(1 for d in devices if d["fix"]) if devices else 0
        if devices:
            log.info("%d targets, %d fixed", len(devices), fixes)


async def send_channel_to_pis(channel):
    """Forward channel command to both Pi dish_clients."""
    cmd = json.dumps({"cmd": "set_channel", "channel": channel})
    for did, ws in list(dish_ws.items()):
        try:
            await ws.send(cmd)
            log.info("Sent channel=%s to %s", channel, did)
        except Exception:
            pass


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
                        if data.get("type") == "wipe":
                            engine.devices.clear()
                            engine.ssid_map.clear()
                            engine.path_history.clear()
                            engine.uuid_map.clear()
                            log.info("Data wiped")

                        elif data.get("type") == "config":
                            if "distance" in data:
                                dish_config["distance"] = float(data["distance"])
                            if "bearing" in data:
                                dish_config["bearing"] = float(data["bearing"])
                            if "channel" in data:
                                await send_channel_to_pis(data["channel"])
                            log.info("Config: dist=%.1fm bearing=%.1f deg channel=%s",
                                     dish_config["distance"], dish_config["bearing"],
                                     data.get("channel", "?"))
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
