#!/usr/bin/env bash
# WADAR startup script
# Deploys to Pis, starts all services, begins triangulating.
#
# Usage:
#   ./start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill any existing WADAR processes first
"$SCRIPT_DIR/stop.sh" 2>/dev/null || true
sleep 1

PI4_IP="192.168.1.101"
PI3_IP="192.168.1.103"
PI4_USER="wadar1"
PI3_USER="wadar2"
PI_PASS="raspberry"

SSH="sshpass -p $PI_PASS ssh -o PubkeyAuthentication=no -o ConnectTimeout=10 -o ServerAliveInterval=5"
SCP="sshpass -p $PI_PASS scp -o PubkeyAuthentication=no -o ConnectTimeout=10"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

log()  { echo -e "${CYN}[WADAR]${RST} $*"; }
ok()   { echo -e "${GRN}[  OK ]${RST} $*"; }
warn() { echo -e "${YLW}[ WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERROR]${RST} $*"; }

cleanup() {
    log "Shutting down..."
    kill $TRI_PID $WB_PID $APP_PID 2>/dev/null || true
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

# ── Check Pi connectivity ─────────────────────────────────────────

log "Checking Pi connectivity..."
ping -c 1 -W 2 "$PI4_IP" >/dev/null 2>&1 && ok "Pi 4 ($PI4_IP) reachable" || { err "Pi 4 ($PI4_IP) unreachable"; exit 1; }
ping -c 1 -W 2 "$PI3_IP" >/dev/null 2>&1 && ok "Pi 3 ($PI3_IP) reachable" || { err "Pi 3 ($PI3_IP) unreachable"; exit 1; }

# ── Deploy & start dish_client on Pis ──────────────────────────────

log "Deploying dish_client.py to Pis..."
$SCP "$SCRIPT_DIR/pi_code/dish_client.py" "$PI4_USER@$PI4_IP:~/dish_client.py" && ok "Pi 4 deployed" || { err "Pi 4 deploy failed"; exit 1; }
$SCP "$SCRIPT_DIR/pi_code/dish_client.py" "$PI3_USER@$PI3_IP:~/dish_client.py" && ok "Pi 3 deployed" || { err "Pi 3 deploy failed"; exit 1; }

log "Starting dish_client on Pis..."
$SSH "$PI3_USER@$PI3_IP" "sudo fuser -k 8082/tcp 2>/dev/null; true"
$SSH "$PI3_USER@$PI3_IP" "sudo nohup python3 -u ~/dish_client.py DISH-1 8082 0 wlan1 </dev/null >~/dish_client.log 2>&1 &"

$SSH "$PI4_USER@$PI4_IP" "sudo fuser -k 8082/tcp 2>/dev/null; true"
$SSH "$PI4_USER@$PI4_IP" "sudo nohup python3 -u ~/dish_client.py DISH-2 8082 0 wlan1 </dev/null >~/dish_client.log 2>&1 &"

log "Waiting for dish_clients to start..."
sleep 5

$SSH "$PI3_USER@$PI3_IP" "grep -q 'Listening' ~/dish_client.log" && ok "DISH-1 (Pi 3) running" || warn "DISH-1 may still be starting"
$SSH "$PI4_USER@$PI4_IP" "grep -q 'Listening' ~/dish_client.log" && ok "DISH-2 (Pi 4) running" || warn "DISH-2 may still be starting"

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

# ── Start triangulator ─────────────────────────────────────────────

log "Starting triangulator..."
python3 frontend/triangulator.py >/tmp/wadar-tri.log 2>&1 &
TRI_PID=$!
sleep 2
ok "Triangulator (PID $TRI_PID)"

# ── Status display ─────────────────────────────────────────────────

echo ""
echo -e "${GRN}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${GRN}║              WADAR SYSTEM RUNNING                   ║${RST}"
echo -e "${GRN}╠══════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Dashboard:  ${CYN}http://localhost:5002/wadar${RST}             ${GRN}║${RST}"
echo -e "${GRN}║${RST}  DISH-1:     ${CYN}$PI4_IP:8082${RST}                       ${GRN}║${RST}"
echo -e "${GRN}║${RST}  DISH-2:     ${CYN}$PI3_IP:8082${RST}                       ${GRN}║${RST}"
echo -e "${GRN}║${RST}  Channel:    ${CYN}6${RST} (change via UI)                       ${GRN}║${RST}"
echo -e "${GRN}╠══════════════════════════════════════════════════════╣${RST}"
echo -e "${GRN}║${RST}  Press Ctrl+C to stop all services                  ${GRN}║${RST}"
echo -e "${GRN}╚══════════════════════════════════════════════════════╝${RST}"
echo ""

# ── Tail triangulator log ──────────────────────────────────────────

tail -f /tmp/wadar-tri.log
