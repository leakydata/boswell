#!/usr/bin/env bash
# Build the Zephyr firmware and flash it only if the build succeeded.
#
# Exists because a chained "build; flash" once flashed a stale hex after a
# compile error and the board was then tested against firmware that did not
# contain the change under test. Failing loudly beats measuring the wrong
# binary.
set -euo pipefail

cd "$(dirname "$0")/../firmware/zephyr"
./build.sh
HEX=/tmp/boswell-zephyr-build/zephyr/zephyr.hex
[ -f "$HEX" ] || { echo "no hex produced" >&2; exit 1; }

# Newer than the build start, so a stale artifact cannot be flashed silently.
find "$HEX" -newermt '-5 minutes' -print -quit | grep -q . \
  || { echo "hex is stale -- build did not regenerate it" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
python3 - <<'PY' || true
import serial, time
try:
    # Find the shell rather than assume a port. The device exposes two CDC
    # ports and which one carries the shell moves between reflashes; a
    # hardcoded path meant the DFU request went to the wrong port and the
    # flash silently did nothing while reporting a failure that looked like
    # the bootloader's fault.
    import glob
    for port in sorted(glob.glob('/dev/ttyACM*')):
        try:
            s = serial.Serial(port, 115200, timeout=1)
            s.reset_input_buffer()
            s.write(b'\r\nboswell status\r\n'); time.sleep(1.5)
            if b'link=' not in s.read(4000):
                s.close(); continue
            s.write(b'\r\nboswell dfu\r\n'); time.sleep(1); s.close()
            break
        except Exception:
            pass
except Exception:
    pass   # already in the bootloader, or running the Arduino build
PY
sleep 5
# Serial DFU first, then the UF2 mass-storage drive. The bootloader exposes
# both, and which one answers is not reliable: serial DFU intermittently
# reports "not in DFU mode" on a board that is plainly sitting in the
# bootloader with its drive mounted. Falling back beats a failed flash.
adafruit-nrfutil dfu genpkg --dev-type 0x0052 --sd-req 0x0123 \
  --application "$HEX" /tmp/boswell.zip >/dev/null

# The bootloader's DFU port is whichever one appears after the reboot; try
# each rather than assuming the first.
for port in /dev/ttyACM*; do
  [ -e "$port" ] || continue
  if adafruit-nrfutil dfu serial -pkg /tmp/boswell.zip -p "$port" \
       -b 115200 --singlebank 2>/dev/null | tail -1 | grep -q "Device programmed"; then
    echo "flashed over serial DFU ($port)"
    exit 0
  fi
done

UF2=/tmp/boswell-zephyr-build/zephyr/zephyr.uf2
DEV=""
for _ in $(seq 1 20); do
  DEV=$(lsblk -o NAME,LABEL -nr 2>/dev/null | awk '$2 ~ /^(XIAO|NRF52BOOT|FTHR)/ {print "/dev/"$1; exit}')
  [ -n "$DEV" ] && break
  sleep 1
done
[ -n "$DEV" ] || { echo "no bootloader drive and serial DFU failed" >&2; exit 1; }

sudo mkdir -p /mnt/xiao
sudo umount /mnt/xiao 2>/dev/null || true
sudo mount "$DEV" /mnt/xiao
sudo cp "$UF2" /mnt/xiao/ && sync
sleep 3
sudo umount /mnt/xiao 2>/dev/null || true
echo "flashed over UF2"
