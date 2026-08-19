#!/usr/bin/env bash
# Forget the device and restart BlueZ.
#
# BlueZ caches a peripheral's GATT table by address. Reflashing changes the
# table but not the address, so the cached copy no longer matches and
# connections time out with nothing useful in the error. Needed after most
# firmware updates.
set -u
ADDR="${1:-E4:5A:24:9D:01:B2}"
bluetoothctl disconnect "$ADDR" >/dev/null 2>&1
bluetoothctl remove "$ADDR" >/dev/null 2>&1
sudo systemctl restart bluetooth
sleep 4
bluetoothctl power on >/dev/null 2>&1
sleep 2
echo "bluetooth reset; $ADDR forgotten"
