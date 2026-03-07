#!/bin/bash
# Build and install rtl8812au driver on Raspberry Pi.
# Works on both Pi 3 (armhf) and Pi 4 (arm64).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARBALL="$SCRIPT_DIR/rtl8812au.tar.gz"
BUILD_DIR="/tmp/rtl8812au-build"

if [ "$(id -u)" -ne 0 ]; then
    echo "Must run as root"
    exit 1
fi

# Detect architecture
ARCH=$(uname -m)
echo "Architecture: $ARCH"
echo "Kernel: $(uname -r)"

# Clean previous build
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# Extract
echo "Extracting driver source..."
tar xzf "$TARBALL"
cd rtl8812au-*

# Set platform config in Makefile
# Disable x86 default, enable the right Pi platform
sed -i 's/^CONFIG_PLATFORM_I386_PC = y/CONFIG_PLATFORM_I386_PC = n/' Makefile

case "$ARCH" in
    aarch64)
        echo "Configuring for Pi 4 (arm64)..."
        sed -i 's/^CONFIG_PLATFORM_ARM64_RPI = n/CONFIG_PLATFORM_ARM64_RPI = y/' Makefile
        ;;
    armv7l)
        echo "Configuring for Pi 3 (armhf)..."
        sed -i 's/^CONFIG_PLATFORM_ARM_RPI = n/CONFIG_PLATFORM_ARM_RPI = y/' Makefile
        ;;
    *)
        echo "Unknown architecture: $ARCH"
        exit 1
        ;;
esac

# Build
echo "Building (this takes a few minutes on a Pi)..."
make -j$(nproc)

# Install
echo "Installing module..."
make install
depmod -a

# Load
echo "Loading 88XXau module..."
modprobe 88XXau

echo ""
echo "=== Verification ==="
lsmod | grep 88XXau && echo "Driver loaded OK" || echo "WARN: driver not in lsmod"

# Check if the adapter created a new interface
echo ""
echo "WiFi interfaces:"
iw dev
echo ""
echo "Done. If wlan1 appeared, run: sudo python3 $SCRIPT_DIR/monitor_capture.py wlan1"
