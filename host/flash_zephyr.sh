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
    s = serial.Serial('/dev/ttyACM1', 115200, timeout=1)
    s.write(b'\r\nboswell dfu\r\n'); time.sleep(1); s.close()
except Exception:
    pass   # already in the bootloader, or running the Arduino build
PY
sleep 5
adafruit-nrfutil dfu genpkg --dev-type 0x0052 --sd-req 0x0123 \
  --application "$HEX" /tmp/boswell.zip >/dev/null
adafruit-nrfutil dfu serial -pkg /tmp/boswell.zip -p /dev/ttyACM0 -b 115200 --singlebank | tail -1
