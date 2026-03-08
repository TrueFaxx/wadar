#!/usr/bin/env bash
# Stop all WADAR services

PI4_IP="192.168.1.101"
PI3_IP="192.168.1.103"
PI4_USER="wadar1"
PI3_USER="wadar2"
PI_PASS="raspberry"
SSH="sshpass -p $PI_PASS ssh -o PubkeyAuthentication=no -o ConnectTimeout=5"

echo "Stopping WADAR..."

# Local processes
pkill -f "python3 frontend/wb.py" 2>/dev/null
pkill -f "python3 frontend/app.py" 2>/dev/null
pkill -f "python3 frontend/triangulator.py" 2>/dev/null
fuser -k 5002/tcp 5003/tcp 5004/tcp 5005/tcp 2>/dev/null

# Pi processes
$SSH "$PI4_USER@$PI4_IP" "sudo fuser -k 8082/tcp 2>/dev/null; true" 2>/dev/null
$SSH "$PI3_USER@$PI3_IP" "sudo fuser -k 8082/tcp 2>/dev/null; true" 2>/dev/null

echo "All WADAR processes stopped."
