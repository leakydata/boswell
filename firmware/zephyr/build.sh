#!/usr/bin/env bash
# Build the Zephyr firmware and, with --flash, put it on the board.
#
# Expects an nRF Connect SDK west workspace and a Zephyr SDK. Set NCS_DIR and
# ZEPHYR_SDK_DIR if yours are elsewhere.
set -euo pipefail

NCS_DIR="${NCS_DIR:-$HOME/ncs}"
ZEPHYR_SDK_DIR="${ZEPHYR_SDK_DIR:-$HOME/zephyr-sdk}"
BOARD="${BOARD:-xiao_ble/nrf52840/sense}"
BUILD_DIR="${BUILD_DIR:-/tmp/boswell-zephyr-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -d "$NCS_DIR/zephyr" ] || { echo "No Zephyr at $NCS_DIR/zephyr. Set NCS_DIR." >&2; exit 1; }
[ -d "$ZEPHYR_SDK_DIR" ] || { echo "No Zephyr SDK at $ZEPHYR_SDK_DIR. Set ZEPHYR_SDK_DIR." >&2; exit 1; }

export ZEPHYR_BASE="$NCS_DIR/zephyr"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_SDK_INSTALL_DIR="$ZEPHYR_SDK_DIR"
[ -d "$NCS_DIR/.venv" ] && export PATH="$NCS_DIR/.venv/bin:$PATH"

west build -p auto -b "$BOARD" -d "$BUILD_DIR" "$HERE/boswell"

UF2="$BUILD_DIR/boswell/zephyr/zephyr.uf2"
echo "built: $UF2"

if [ "${1:-}" = "--flash" ]; then
  # Same route as the Arduino build: the board definition already targets the
  # Adafruit bootloader at 0x27000 and emits a UF2 with the right family id.
  exec "$HERE/../../host/flash.sh" "$UF2"
fi
