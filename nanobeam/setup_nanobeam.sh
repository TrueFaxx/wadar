#!/usr/bin/env bash
# NanoBeam AC Gen2 setup helper
#
# Tests connectivity to your NanoBeams and configures them for WADAR.
# Run this BEFORE start_nanobeam.sh to verify everything works.
#
# Usage:
#   ./setup_nanobeam.sh [nanobeam1_ip] [nanobeam2_ip]

set -e

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
CYN='\033[0;36m'
RST='\033[0m'

log()  { echo -e "${CYN}[SETUP]${RST} $*"; }
ok()   { echo -e "${GRN}[  OK ]${RST} $*"; }
warn() { echo -e "${YLW}[ WARN]${RST} $*"; }
err()  { echo -e "${RED}[ERROR]${RST} $*"; }

NB1_IP="${1:-192.168.1.20}"
NB2_IP="${2:-192.168.1.21}"
NB_USER="${NB_USER:-ubnt}"
NB_PASS="${NB_PASS:-ubnt}"

SSH="sshpass -p $NB_PASS ssh -o PubkeyAuthentication=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"

echo ""
echo -e "${CYN}╔══════════════════════════════════════════════════════╗${RST}"
echo -e "${CYN}║       WADAR - NanoBeam AC Gen2 Setup                ║${RST}"
echo -e "${CYN}╚══════════════════════════════════════════════════════╝${RST}"
echo ""

# ── Check dependencies ────────────────────────────────────────────

log "Checking local dependencies..."
for dep in python3 sshpass ping; do
    if command -v "$dep" >/dev/null 2>&1; then
        ok "$dep found"
    else
        err "$dep not found — install it first"
        exit 1
    fi
done

python3 -c "import websockets" 2>/dev/null && ok "python3 websockets" || { err "pip install websockets"; exit 1; }
python3 -c "import flask" 2>/dev/null && ok "python3 flask" || { err "pip install flask"; exit 1; }

echo ""

# ── Check NanoBeam connectivity ───────────────────────────────────

log "Testing network connectivity..."

check_nanobeam() {
    local ip=$1
    local label=$2

    echo ""
    log "── $label ($ip) ──"

    # Ping
    if ping -c 1 -W 3 "$ip" >/dev/null 2>&1; then
        ok "Ping OK"
    else
        err "Cannot ping $ip"
        echo "    Make sure the NanoBeam is powered on and connected to your network."
        echo "    Default NanoBeam IP is 192.168.1.20 — you may need to set a static IP"
        echo "    on your computer in the 192.168.1.x range to access it initially."
        return 1
    fi

    # SSH
    local version
    version=$($SSH "$NB_USER@$ip" "cat /etc/version" 2>/dev/null)
    if [ -n "$version" ]; then
        ok "SSH OK — AirOS $version"
    else
        err "SSH failed"
        echo "    Default credentials: ubnt / ubnt"
        echo "    Set NB_USER and NB_PASS env vars if you changed them."
        echo "    Make sure SSH is enabled in the AirOS web UI: System > Device Management"
        return 1
    fi

    # Device info
    local board
    board=$($SSH "$NB_USER@$ip" "cat /tmp/board.info 2>/dev/null | head -5" 2>/dev/null)
    if [ -n "$board" ]; then
        echo -e "    ${CYN}Device info:${RST}"
        echo "$board" | sed 's/^/      /'
    fi

    # WiFi interface
    local ifaces
    ifaces=$($SSH "$NB_USER@$ip" "iwconfig 2>/dev/null | grep 'IEEE'" 2>/dev/null)
    if [ -n "$ifaces" ]; then
        ok "WiFi interfaces found:"
        echo "$ifaces" | sed 's/^/      /'
    else
        warn "No WiFi interfaces found via iwconfig"
    fi

    # Test station list
    local wsta
    wsta=$($SSH "$NB_USER@$ip" "wstalist 2>/dev/null" 2>/dev/null)
    if [ -n "$wsta" ]; then
        local count
        count=$(echo "$wsta" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "?")
        ok "wstalist works — $count associated stations"
    else
        warn "wstalist returned empty (normal if no clients connected)"
    fi

    return 0
}

NB1_OK=0
NB2_OK=0
check_nanobeam "$NB1_IP" "NanoBeam 1 (DISH-1)" && NB1_OK=1
check_nanobeam "$NB2_IP" "NanoBeam 2 (DISH-2)" && NB2_OK=1

echo ""
echo -e "${CYN}══════════════════════════════════════════════════════${RST}"
echo ""

# ── Summary ───────────────────────────────────────────────────────

if [ $NB1_OK -eq 1 ] && [ $NB2_OK -eq 1 ]; then
    ok "Both NanoBeams are reachable and ready!"
    echo ""
    echo "  Next steps:"
    echo ""
    echo "  1. Mount NanoBeams at known positions, pointed in different directions"
    echo "     - Ideal: 60-120 degree angle between them"
    echo "     - Use a compass or compass app to measure each NanoBeam's heading"
    echo "     - Measure the distance between them (in meters)"
    echo ""
    echo "  2. Edit start_nanobeam.sh with your values:"
    echo "     - NB1_IP / NB2_IP — IP addresses"
    echo "     - NB1_HEADING / NB2_HEADING — compass heading each NanoBeam points"
    echo "     - DISH_DISTANCE — distance between NanoBeams in meters"
    echo "     - DISH_BEARING — compass bearing from NB1 to NB2"
    echo ""
    echo "  3. Run:  ./start_nanobeam.sh"
    echo ""
elif [ $NB1_OK -eq 1 ] || [ $NB2_OK -eq 1 ]; then
    warn "Only one NanoBeam is reachable."
    echo "    You can still run WADAR with one dish for signal detection,"
    echo "    but triangulation requires both."
else
    err "Neither NanoBeam is reachable."
    echo ""
    echo "  Troubleshooting:"
    echo "  1. Connect your computer to the same network/switch as the NanoBeams"
    echo "  2. NanoBeam default IP: 192.168.1.20 (set your IP to 192.168.1.x)"
    echo "  3. Access the AirOS web UI at https://<nanobeam_ip>"
    echo "  4. Default login: ubnt / ubnt"
    echo "  5. Enable SSH: System > Device Management > SSH Server"
    echo "  6. Set a static IP or configure DHCP"
fi

echo ""
