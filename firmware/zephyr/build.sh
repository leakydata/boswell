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

# --no-sysbuild is load-bearing. With sysbuild, NCS's Partition Manager
# overrides the board's CONFIG_FLASH_LOAD_OFFSET and links the image at 0x0.
# The Adafruit bootloader then writes it at 0x27000, so the vector table sits
# where the code was not built for and the board hard-faults on boot: no LED,
# no USB, nothing to debug with.
# Optional extras, for a build that is not the plain wearable: a second
# devicetree overlay and a second Kconfig fragment, both applied on top of the
# board's own rather than replacing them. The Audio BFF build uses both:
#
#   EXTRA_OVERLAY=boards/audio_bff.overlay EXTRA_CONF=sdcard.conf build.sh
#
# Paths are relative to the application directory. Unset, nothing changes and
# the wearable builds byte-for-byte as before.
EXTRA_ARGS=()
if [ -n "${EXTRA_OVERLAY:-}" ]; then
  EXTRA_ARGS+=(-DEXTRA_DTC_OVERLAY_FILE="$HERE/boswell/$EXTRA_OVERLAY")
fi
if [ -n "${EXTRA_CONF:-}" ]; then
  EXTRA_ARGS+=(-DEXTRA_CONF_FILE="$HERE/boswell/$EXTRA_CONF")
fi

west build -p auto --no-sysbuild -b "$BOARD" -d "$BUILD_DIR" "$HERE/boswell" \
     "${EXTRA_ARGS[@]}"

UF2="$BUILD_DIR/zephyr/zephyr.uf2"
echo "built: $UF2"

if [ "${1:-}" = "--flash" ]; then
  # Same route as the Arduino build: the board definition already targets the
  # Adafruit bootloader at 0x27000 and emits a UF2 with the right family id.
  exec "$HERE/../../host/flash.sh" "$UF2"
fi
