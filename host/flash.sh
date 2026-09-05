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
  # How long to keep asking depends on what is on the board.
  #
  # A board running this firmware calls itself Boswell on the USB bus, and a
  # board running this firmware has a shell -- so if that name is there, the
  # silence is temporary and worth waiting out. The CDC interfaces take a few
  # seconds to come back after a reset, and the second of two back-to-back
  # flashes probed inside that window, found nothing, and fell through to the
  # touch on a board that would have answered a second later. Anything else --
  # an Arduino build, a factory board, a bricked one -- gets a single pass and
  # then the touch, because for those there is nothing to wait for.
  local deadline=$SECONDS
  if grep -qs "Boswell" /sys/bus/usb/devices/*/product; then
    deadline=$((SECONDS + 15))
  fi
  while :; do
    for p in /dev/ttyACM*; do
      [ -e "$p" ] || continue
      stty -F "$p" raw -echo 115200 2>/dev/null || continue
      exec 3<>"$p" || continue
      # Opening the port is what raises DTR, and Zephyr's CDC ACM shell backend
      # says nothing until it sees that. Writing immediately reads an empty port
      # on a shell that is perfectly alive.
      sleep 0.5
      printf '\r\n' >&3
      # Read with bash's own read rather than `timeout head -c`.
      #
      # head buffers through stdio and never flushes what it is holding when
      # timeout kills it, so a prompt that did arrive was read and then thrown
      # away. This probe therefore reported "no Zephyr shell found" on a board
      # whose shell was answering, and fell through to the 1200-baud touch --
      # which, as the comment above says, Zephyr does not implement. The result
      # was two hundred seconds of waiting for a bootloader nothing had asked
      # for. The shell is on the second CDC interface, so the port that stays
      # silent here is expected, not a fault.
      resp=""
      n=0
      while [ "$n" -lt 400 ] && IFS= read -r -t 2 -n 1 ch <&3; do
        resp="$resp$ch"
        n=$((n + 1))
        case "$resp" in *"boswell>"*) break ;; esac
      done
      case "$resp" in
        *"boswell>"*)
          echo "Zephyr shell on $p — asking the board to reboot into the bootloader"
          printf 'boswell dfu\r\n' >&3
          # Let the write drain before dropping the port.
          #
          # Closing the fd straight after the printf left the command sitting
          # in a buffer: the board did not reboot until the retry twenty
          # seconds later reopened the port and flushed it, so every flash
          # paid an extra twenty seconds and the retry looked like the thing
          # that had worked. The board reboots the moment it reads this, so
          # the port disappears underneath us and the wait is self-limiting.
          sleep 1
          exec 3<&-
          return 0
          ;;
      esac
      exec 3<&-
    done
    [ "$SECONDS" -lt "$deadline" ] || break
    sleep 1
  done
  # No shell answered: an Arduino build, or a board that is not talking.
  if [ -e "$PORT" ]; then
    echo "no Zephyr shell found; trying the 1200-baud touch on $PORT ..."
    stty -F "$PORT" 1200 hupcl 2>/dev/null
    (exec 3<>"$PORT"; sleep 0.3; exec 3<&-) 2>/dev/null
  fi
}
# Falling back to serial DFU when the drive never turns up.
#
# A double-tap and the 1200-baud touch do not leave the board in the same
# place. The double-tap sets GPREGRET to 0x57 and the bootloader comes up in
# UF2 mode, with the mass storage drive the loop below is looking for. The
# touch sets 0x4e -- serial DFU only -- so the board enumerates as 2886:0045
# with a CDC port and nothing else, and that loop sits through its full two
# hundred seconds waiting for a block device that was never coming. A
# replacement board did exactly this on its first flash: it was in the
# bootloader the whole time, and the script reported it as a timeout.
#
# The bootloader takes the firmware over that CDC port perfectly well, so send
# it rather than telling somebody to go and press a button. adafruit-nrfutil
# wants a hex rather than a UF2; the Zephyr build writes both side by side.
try_serial_dfu() {
  if ! command -v adafruit-nrfutil >/dev/null 2>&1; then
    echo "board is in serial-DFU mode but adafruit-nrfutil is not installed" >&2
    echo "  pip install --user adafruit-nrfutil, or double-tap RESET for UF2 mode" >&2
    return 1
  fi
  local hex="${HEX:-${UF2%.uf2}.hex}"
  if [ ! -f "$hex" ]; then
    echo "board is in serial-DFU mode but there is no hex beside the UF2: $hex" >&2
    echo "  set HEX=/path/to/firmware.hex, or double-tap RESET for UF2 mode" >&2
    return 1
  fi
  # The bootloader's CDC port is not necessarily the one the application was
  # on, so find it by USB product id rather than reusing $PORT.
  local port="" p
  for p in /dev/ttyACM*; do
    [ -e "$p" ] || continue
    if udevadm info -q property -n "$p" 2>/dev/null | grep -q "^ID_MODEL_ID=0045$"; then
      port="$p"; break
    fi
  done
  [ -n "$port" ] || port="$PORT"
  [ -e "$port" ] || { echo "no serial port to send DFU over" >&2; return 1; }

  local tmp zip
  tmp="$(mktemp -d)" || return 1
  zip="$tmp/boswell_dfu.zip"
  echo "no UF2 drive, but the board is in serial-DFU mode -- sending $(basename "$hex") over $port"
  if ! adafruit-nrfutil dfu genpkg --dev-type 0x0052 --application "$hex" "$zip" >/dev/null; then
    echo "could not package $hex for DFU" >&2; rm -rf "$tmp"; return 1
  fi
  if ! adafruit-nrfutil dfu serial --package "$zip" -p "$port" -b 115200 --singlebank; then
    echo "serial DFU failed" >&2; rm -rf "$tmp"; return 1
  fi
  rm -rf "$tmp"
  return 0
}

enter_bootloader

# Finding the bootloader once it is there.
#
# Matching only on the disk LABEL was not enough, and cost an attempt twice:
# the board reached the bootloader and this loop sat through its full two
# hundred seconds without seeing it, then found it immediately on the next
# run. The label arrives from udev some time after the block device does, so a
# scan that insists on it can miss the window entirely.
#
# So three signals, cheapest first, and the USB id is the authority: 2886:0045
# is the bootloader, and if that is present the drive exists whether or not
# anything has labelled it yet. The dfu request is repeated every twenty
# seconds as well, in case the first one landed while the shell was busy.
echo "waiting for bootloader (double-tap RESET if nothing happens) ..."
DEV=""
BOOT_SEEN=0
SERIAL_TRIED=0
SERIAL_DONE=0
for i in $(seq 1 200); do
  DEV=$(lsblk -o NAME,LABEL -nr 2>/dev/null | awk '$2 ~ /^(XIAO|NRF52BOOT|FTHR)/ {print "/dev/"$1; exit}')
  if [ -z "$DEV" ] && [ -e /dev/disk/by-label/XIAO-SENSE ]; then
    DEV=$(readlink -f /dev/disk/by-label/XIAO-SENSE)
  fi
  if [ -z "$DEV" ] && lsusb 2>/dev/null | grep -q "2886:0045"; then
    # In the bootloader for certain; take the removable disk it presents even
    # if it has no label yet.
    DEV=$(lsblk -o NAME,RM,TYPE -nr 2>/dev/null |
          awk '$2 == 1 && $3 == "disk" {print "/dev/"$1; exit}')
  fi
  # Seen in lsblk is not the same as ready to mount.
  #
  # lsblk reports the device as soon as the kernel enumerates it, which is
  # before udev has created the node -- so this accepted /dev/sdb at three
  # seconds and the mount then failed with "not a valid block device" on a
  # board that was sitting in the bootloader perfectly happily. Requiring a
  # real block node costs nothing and is the difference between a flash that
  # works and one that reports the board is broken when it is not.
  if [ -n "$DEV" ] && [ -b "$DEV" ]; then
    echo "[${i}s] bootloader block device: $DEV"
    break
  fi
  DEV=""
  # In the bootloader, but with no drive: that is serial-DFU mode, and no
  # amount of further waiting will produce a block device. Twelve seconds is
  # well past the udev lag that the check above exists to absorb, so by now
  # the absence means something. One attempt only -- if it cannot run, keep
  # waiting, because a double-tap will still get us the drive.
  if lsusb 2>/dev/null | grep -q "2886:0045"; then
    BOOT_SEEN=$((BOOT_SEEN + 1))
  else
    BOOT_SEEN=0
  fi
  if [ "$BOOT_SEEN" -ge 12 ] && [ "$SERIAL_TRIED" = 0 ]; then
    SERIAL_TRIED=1
    if try_serial_dfu; then SERIAL_DONE=1; break; fi
    echo "[${i}s] serial DFU unavailable; still waiting for the UF2 drive" >&2
  fi
  if [ $((i % 20)) -eq 0 ]; then
    echo "[${i}s] still waiting; asking again"
    enter_bootloader >/dev/null 2>&1
  fi
  sleep 1
done
if [ "$SERIAL_DONE" = 0 ]; then
  [ -n "$DEV" ] || { echo "timed out waiting for bootloader" >&2; exit 1; }

  sudo mkdir -p /mnt/xiao
  sudo umount /mnt/xiao 2>/dev/null
  # The node can exist a moment before the bootloader will answer reads on it,
  # so give it a few tries rather than declaring failure on the first refusal.
  mounted=0
  for try in 1 2 3 4 5 6 7 8 9 10; do
    if sudo mount "$DEV" /mnt/xiao 2>/dev/null; then mounted=1; break; fi
    sleep 1
  done
  [ "$mounted" = 1 ] || { echo "mount failed: $DEV" >&2; exit 1; }
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
fi

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
