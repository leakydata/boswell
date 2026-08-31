#!/usr/bin/env bash
# Flash a UF2 to the XIAO. Tries 1200-baud auto-reset first, then waits for
# the bootloader block device. Mounts it explicitly -- on many desktops the
# UF2 drive does not auto-mount, so waiting on a mountpoint never fires.
set -u
UF2="${1:?usage: flash.sh <file.uf2>}"
PORT="${2:-/dev/ttyACM0}"
[ -f "$UF2" ] || { echo "no such file: $UF2" >&2; exit 1; }

# Getting the board into the bootloader, by whichever route it answers to.
#
# The 1200-baud touch is an Arduino core convention: the core's USB stack
# watches for a host opening the port at 1200 baud and reboots itself. Zephyr
# implements nothing of the sort, so on the firmware this project actually
# ships the touch does nothing at all -- the script sat here for 200 seconds
# telling somebody to double-tap RESET on a board that had a perfectly good
# way to do it itself.
#
# That way is the `dfu` shell command: it writes 0x57 to GPREGRET and resets,
# which is the flag the Adafruit bootloader reads on boot to decide whether to
# stay in UF2 mode. The shell is a USB CDC interface and it is NOT necessarily
# the first one -- on this board it is ttyACM1, while ttyACM0 is silent -- so
# every ACM port is asked rather than assuming.
enter_bootloader() {
  if lsusb 2>/dev/null | grep -q "2886:0045"; then
    echo "board is already in the bootloader"
    return 0
  fi
  for p in /dev/ttyACM*; do
    [ -e "$p" ] || continue
    stty -F "$p" raw -echo 115200 2>/dev/null || continue
    exec 3<>"$p" || continue
    printf '\r\n' >&3
    resp=$(timeout 1.5 head -c 2000 <&3 2>/dev/null | tr -d '\000' || true)
    if printf '%s' "$resp" | grep -q 'boswell>'; then
      echo "Zephyr shell on $p — asking the board to reboot into the bootloader"
      printf 'boswell dfu\r\n' >&3
      exec 3<&-
      return 0
    fi
    exec 3<&-
  done
  # No shell answered: an Arduino build, or a board that is not talking.
  if [ -e "$PORT" ]; then
    echo "no Zephyr shell found; trying the 1200-baud touch on $PORT ..."
    stty -F "$PORT" 1200 hupcl 2>/dev/null
    (exec 3<>"$PORT"; sleep 0.3; exec 3<&-) 2>/dev/null
  fi
}
enter_bootloader

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
