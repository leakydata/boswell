#!/usr/bin/env bash
# Flash the board through whichever bootloader mode it happens to present.
#
# A double-tap of RESET gives UF2 mass storage; a 1200-baud touch from a
# running app gives serial DFU. They look different and need different tools,
# and which one you get is not always predictable -- so try both.
#
#   flash_any.sh <file.uf2> <file.zip> [seconds]
set -u
UF2="${1:?need a .uf2}"
ZIP="${2:?need a DFU .zip}"
SECS="${3:-240}"

echo "waiting up to ${SECS}s for a bootloader (double-tap RESET) ..."
# Poll fast: a single reset shows the bootloader for about 200 ms before it
# jumps to the app, and a one-second poll misses that window entirely.
ITERS=$((SECS * 10))
for i in $(seq 1 "$ITERS"); do
  # UF2 mass storage: the drive does not auto-mount here, so mount it.
  DEV=$(lsblk -o NAME,LABEL -nr 2>/dev/null | awk '$2 ~ /^(XIAO|NRF52BOOT|FTHR)/ {print "/dev/"$1; exit}')
  if [ -n "${DEV:-}" ]; then
    echo "[$((i/10))s] UF2 bootloader on $DEV"
    sudo mkdir -p /mnt/xiao
    sudo umount /mnt/xiao 2>/dev/null
    if sudo mount "$DEV" /mnt/xiao; then
      sudo cp "$UF2" /mnt/xiao/ || echo "copy reported an error; checking the board"
      sync 2>/dev/null || true
      sleep 3
      sudo umount /mnt/xiao 2>/dev/null
      # Ask the board rather than assume. The drive vanishes underneath the
      # copy when the bootloader reboots, so cp can fail on a good flash and
      # succeed on nothing useful; only what is running afterwards settles it.
      for _ in $(seq 1 25); do
        lsusb | grep -q "2886:8045" && { echo "flashed via UF2 (running 2886:8045)"; exit 0; }
        sleep 1
      done
      echo "UF2 copy did not produce a running application" >&2
      exit 1
    fi
  fi
  # Serial DFU: CDC only, no drive.
  if lsusb 2>/dev/null | grep -q "2886:0045" && [ -e /dev/ttyACM0 ]; then
    echo "[$((i/10))s] serial DFU on /dev/ttyACM0"
    sleep 3          # the CDC port needs a moment before DFU will talk to it
    if adafruit-nrfutil dfu serial -pkg "$ZIP" -p /dev/ttyACM0 -b 115200 --singlebank; then
      echo "flashed via serial DFU"
      exit 0
    fi
  fi
  sleep 0.1
done
echo "timed out" >&2
exit 1
