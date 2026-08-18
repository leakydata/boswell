#!/usr/bin/env bash
# Waits to see what the board does after a reset: boot the app, or fall back
# into the bootloader (which would mean the flashed app is crashing).
set -u
echo "watching for 90s — press RESET once now"
for i in $(seq 1 90); do
  ID=$(lsusb 2>/dev/null | grep -oE "2886:(8045|0045)" | head -1)
  if [ "$ID" = "2886:8045" ]; then
    echo "[${i}s] APP RUNNING (2886:8045) — firmware booted fine"
    exit 0
  fi
  sleep 1
done
echo "still in bootloader after 90s (2886:0045)"
exit 1
