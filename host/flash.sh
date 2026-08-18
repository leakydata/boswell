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
sudo cp "$UF2" /mnt/xiao/ && sync
sleep 3
sudo umount /mnt/xiao 2>/dev/null
echo "done — board reboots into the new firmware."
