#!/usr/bin/env bash
# Flash a UF2 to the XIAO. Tries 1200-baud auto-reset first, then waits for
# the bootloader block device. Mounts it explicitly -- on many desktops the
# UF2 drive does not auto-mount, so waiting on a mountpoint never fires.
set -u
UF2="${1:?usage: flash.sh <file.uf2>}"
PORT="${2:-/dev/ttyACM0}"
[ -f "$UF2" ] || { echo "no such file: $UF2" >&2; exit 1; }

if [ -e "$PORT" ]; then
  echo "attempting 1200-baud auto-reset on $PORT ..."
  stty -F "$PORT" 1200 hupcl 2>/dev/null
  (exec 3<>"$PORT"; sleep 0.3; exec 3<&-) 2>/dev/null
fi

echo "waiting for bootloader (double-tap RESET if nothing happens) ..."
DEV=""
for i in $(seq 1 200); do
  DEV=$(lsblk -o NAME,LABEL -nr 2>/dev/null | awk '$2 ~ /^(XIAO|NRF52BOOT|FTHR)/ {print "/dev/"$1; exit}')
  [ -n "$DEV" ] && { echo "[${i}s] bootloader block device: $DEV"; break; }
  sleep 1
done
[ -n "$DEV" ] || { echo "timed out waiting for bootloader" >&2; exit 1; }

sudo mkdir -p /mnt/xiao
sudo umount /mnt/xiao 2>/dev/null
sudo mount "$DEV" /mnt/xiao || { echo "mount failed" >&2; exit 1; }
echo "flashing $(basename "$UF2") ($(stat -c%s "$UF2") bytes) ..."
# cp's exit status is not the question here.
#
# A UF2 write ends with the bootloader rebooting into the new firmware, so the
# drive disappears underneath the copy and cp can report an I/O error on a
# flash that worked. It can equally fail for real. This script used to run
# `cp ... && sync` and then unconditionally print that the board had rebooted
# into the new firmware, exiting zero either way, so a failed flash was
# indistinguishable from a good one -- and a later measurement would be taken
# against whatever was on the board before.
#
# So: ask the board what it is running. 2886:0045 is the bootloader and
# 2886:8045 is the application.
sudo cp "$UF2" /mnt/xiao/ || echo "copy reported an error; checking the board anyway"
sync 2>/dev/null || true
sleep 3
sudo umount /mnt/xiao 2>/dev/null

for _ in $(seq 1 25); do
  if lsusb | grep -q "2886:8045"; then
    echo "done — board is running the application (2886:8045)."
    exit 0
  fi
  sleep 1
done
echo "flash did not take: the board is not running an application" >&2
lsusb | grep 2886 >&2 || true
exit 1
