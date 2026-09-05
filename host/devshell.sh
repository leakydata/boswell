#!/usr/bin/env bash
# Send one command to the device's shell and print the reply.
#
#   host/devshell.sh "boswell status"
#   host/devshell.sh "boswell pins 20" 25      # second arg: seconds to listen
#
# The port moves between /dev/ttyACM1 and /dev/ttyACM2 across resets, so it is
# discovered rather than assumed -- hardcoding it is how a working command
# starts reporting a dead board.
set -u
cmd="${1:?usage: devshell.sh \"boswell status\" [seconds]}"
wait_s="${2:-6}"

for port in /dev/ttyACM2 /dev/ttyACM1 /dev/ttyACM0; do
  [ -e "$port" ] || continue
  stty -F "$port" raw -echo 115200 2>/dev/null || continue
  exec 3<>"$port" || continue

  printf '\r\n' >&3
  sleep 0.2
  printf '%s\r\n' "$cmd" >&3

  out=""
  end=$(( $(date +%s) + wait_s ))
  while [ "$(date +%s)" -lt "$end" ]; do
    if IFS= read -r -t 1 -u 3 line; then out+="$line"$'\n'; fi
  done
  exec 3<&-

  # A port that answered anything at all is the one; the others are the
  # bootloader's or a different device.
  if printf '%s' "$out" | grep -q "boswell"; then
    printf '%s' "$out" | sed -e 's/\x1b\[[0-9;]*m//g' -e 's/\r//g'
    exit 0
  fi
done

echo "no Boswell shell answered on /dev/ttyACM*" >&2
exit 1
