#!/usr/bin/env bash
# WADAR startup script for NanoBeam AC Gen2 hardware
#
# Instead of Raspberry Pis with BNO055 IMUs, this uses two Ubiquiti
# NanoBeam AC Gen2 directional antennas with fixed headings, polled
# via SSH from a central machine (laptop, COR 1A, etc).
#
# Usage:
#   ./start_nanobeam.sh
#
# Before running:
#   1. Run ./nanobeam/setup_nanobeam.sh to verify connectivity
#   2. Set the heading/IP/distance values below

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any existing WADAR processes first
"$SCRIPT_DIR/stop.sh" 2>/dev/null || true
sleep 1

# ══════════════════════════════════════════════════════════════════
# CONFIGURATION — Edit these for your setup
# ══════════════════════════════════════════════════════════════════

# NanoBeam 1 (DISH-1)
NB1_IP="192.168.1.20"
NB1_HEADING="0"       # Compass degrees the NanoBeam physically points (0=North)

# NanoBeam 2 (DISH-2)
NB2_IP="192.168.1.21"
NB2_HEADING="90"      # Compass degrees the NanoBeam physically points

# SSH credentials (AirOS default: ubnt/ubnt)
NB_USER="ubnt"
NB_PASS="ubnt"

# Geometry between the two NanoBeams
DISH_DISTANCE="5.0"   # Meters between NanoBeam 1 and NanoBeam 2
DISH_BEARING="90"     # Compass bearing FROM NanoBeam 1 TO NanoBeam 2

# Server: where to run Flask + triangulator
# "local" = this machine, or set to an IP to run remotely
SERVER="local"

# ══════════════════════════════════════════════════════════════════

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

log()  { echo -e "${CYN}[WADAR]${RST} $*"; }
ok()   { echo -e "${GRN}[  OK ]${RST} $*"; }
warn() { echo -e "${YLW}[ WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERROR]${RST} $*"; }

NB1_PID=""
NB2_PID=""
WB_PID=""
APP_PID=""
TRI_PID=""

cleanup() {
    log "Shutting down..."
    kill $NB1_PID $NB2_PID $TRI_PID $WB_PID $APP_PID 2>/dev/null || true
    wait 2>/dev/null
    log "Done."
}
trap cleanup EXIT

# ── Check dependencies ────────────────────────────────────────────

check_dep() {
    command -v "$1" >/dev/null 2>&1 || { err "$1 not found"; exit 1; }
}
check_dep python3
check_dep sshpass

python3 -c "import websockets" 2>/dev/null || { err "python3 websockets not installed (pip install websockets)"; exit 1; }
python3 -c "import flask" 2>/dev/null || { err "python3 flask not installed (pip install flask)"; exit 1; }

# ── Check NanoBeam connectivity ───────────────────────────────────

log "Checking NanoBeam connectivity..."

NB1_REACHABLE=0
NB2_REACHABLE=0

ping -c 1 -W 3 "$NB1_IP" >/dev/null 2>&1 && { ok "NanoBeam 1 ($NB1_IP) reachable"; NB1_REACHABLE=1; } || warn "NanoBeam 1 ($NB1_IP) unreachable — will retry in background"
ping -c 1 -W 3 "$NB2_IP" >/dev/null 2>&1 && { ok "NanoBeam 2 ($NB2_IP) reachable"; NB2_REACHABLE=1; } || warn "NanoBeam 2 ($NB2_IP) unreachable — will retry in background"

if [ $NB1_REACHABLE -eq 0 ] && [ $NB2_REACHABLE -eq 0 ]; then
    err "Neither NanoBeam is reachable. Run ./nanobeam/setup_nanobeam.sh for help."
    exit 1
fi

# ── Start NanoBeam clients ────────────────────────────────────────

log "Starting NanoBeam dish clients..."

python3 "$SCRIPT_DIR/nanobeam/nanobeam_client.py" \
    DISH-1 "$NB1_IP" "$NB1_HEADING" 8082 "$NB_USER" "$NB_PASS" \
    >/tmp/wadar-nb1.log 2>&1 &
NB1_PID=$!
ok "DISH-1 client (PID $NB1_PID) -> NanoBeam $NB1_IP heading ${NB1_HEADING}deg"

python3 "$SCRIPT_DIR/nanobeam/nanobeam_client.py" \
    DISH-2 "$NB2_IP" "$NB2_HEADING" 8083 "$NB_USER" "$NB_PASS" \
    >/tmp/wadar-nb2.log 2>&1 &
NB2_PID=$!
ok "DISH-2 client (PID $NB2_PID) -> NanoBeam $NB2_IP heading ${NB2_HEADING}deg"

sleep 2

# ── Start local servers ────────────────────────────────────────────

log "Starting local servers..."

cd "$SCRIPT_DIR"
python3 frontend/wb.py >/tmp/wadar-wb.log 2>&1 &
WB_PID=$!
sleep 1
ok "WebSocket server (PID $WB_PID) :5003/:5004/:5005"

python3 frontend/app.py >/tmp/wadar-app.log 2>&1 &
APP_PID=$!
sleep 1
ok "Web UI (PID $APP_PID) http://localhost:5002/wadar"

# ── Start triangulator (pointed at local NB clients, not remote Pis) ──

log "Starting triangulator..."
# NanoBeam clients run locally on ports 8082 and 8083
python3 frontend/triangulator.py "localhost" "localhost" 8082 8083 >/tmp/wadar-tri.log 2>&1 &
TRI_PID=$!
sleep 2
ok "Triangulator (PID $TRI_PID)"

# ── Status display ─────────────────────────────────────────────────

echo ""
echo -e "${GRN}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${GRN}║          WADAR SYSTEM RUNNING (NanoBeam)            ║${RST}"
echo -e "${GRN}╠══════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Dashboard:  ${CYN}http://localhost:5002/wadar${RST}             ${GRN}║${RST}"
echo -e "${GRN}║${RST}  DISH-1:     ${CYN}NanoBeam $NB1_IP @ ${NB1_HEADING}deg${RST}            ${GRN}║${RST}"
echo -e "${GRN}║${RST}  DISH-2:     ${CYN}NanoBeam $NB2_IP @ ${NB2_HEADING}deg${RST}            ${GRN}║${RST}"
echo -e "${GRN}║${RST}  Distance:   ${CYN}${DISH_DISTANCE}m${RST}  Bearing: ${CYN}${DISH_BEARING}deg${RST}             ${GRN}║${RST}"
echo -e "${GRN}╠══════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Logs:                                               ${GRN}║${RST}"
echo -e "${GRN}║${RST}    NB1: ${CYN}/tmp/wadar-nb1.log${RST}                            ${GRN}║${RST}"
echo -e "${GRN}║${RST}    NB2: ${CYN}/tmp/wadar-nb2.log${RST}                            ${GRN}║${RST}"
echo -e "${GRN}║${RST}    Tri: ${CYN}/tmp/wadar-tri.log${RST}                            ${GRN}║${RST}"
echo -e "${GRN}╠══════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Press Ctrl+C to stop all services                  ${GRN}║${RST}"
echo -e "${GRN}╚══════════════════════════════════════════════════════╝${RST}"
echo ""

# ── Tail triangulator log ──────────────────────────────────────────

tail -f /tmp/wadar-tri.log
