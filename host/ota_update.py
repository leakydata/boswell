#!/usr/bin/env python3
"""
Update the firmware over Bluetooth, no cable.

The Adafruit bootloader this board ships with has a Bluetooth DFU mode that
nothing was using: rebooting with GPREGRET set to 0xA8 rather than the 0x57
that selects mass storage brings it up advertising as "AdaDFU", speaking
Nordic's Legacy DFU protocol. So over-the-air updates need no change to the
bootloader at all, and the UF2 drive stays as the recovery path if an update
ever goes wrong.

    uv run host/ota_update.py /tmp/boswell.zip

The zip is the same package adafruit-nrfutil builds for serial DFU: a .bin
image and a .dat init packet described by manifest.json.
"""

import argparse
import asyncio
import json
import struct
import sys
import zipfile

from bleak import BleakClient, BleakScanner

DFU_SERVICE = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL = "00001531-1212-efde-1523-785feabcd123"
DFU_PACKET  = "00001532-1212-efde-1523-785feabcd123"

DFU_NAME    = "AdaDFU"
APP_NAME    = "XIAO-MIC"
CTRL_UUID   = "4b1a0003-8f2c-4d5e-9a3b-1c7e6f8d0a21"

# Control point opcodes
START_DFU        = 0x01
INITIALIZE_DFU   = 0x02
RECEIVE_FIRMWARE = 0x03
VALIDATE         = 0x04
ACTIVATE_RESET   = 0x05
SYSTEM_RESET     = 0x06
PKT_RECEIPT_REQ  = 0x08
RESPONSE         = 0x10
PKT_RECEIPT      = 0x11

UPDATE_APP = 0x04          # application only, no SoftDevice or bootloader
CHUNK      = 20            # legacy DFU packet size
ERASE_WAIT = 12.0          # seconds to let the app region erase
# No packet-receipt notifications, and every data write is acknowledged.
#
# The bootloader's app_data_process calls hci_mem_pool_rx_produce for each
# packet and reports OPERATION_FAILED if that fails, and its RX pool holds
# one packet at a time -- each has to be consumed, which includes a flash
# write, before the next arrives. A write-without-response stream overruns it
# almost immediately: the first attempt died on packet one, and with receipts
# every ten packets it died at exactly the first checkpoint.
#
# Waiting for each write to be acknowledged throttles the host to the
# bootloader's actual pace. It is slower than a receipt-driven stream would
# be, and it is the difference between an update that works and one that does
# not.
RECEIPT_EVERY = 0
PACE_S = 0


def load_package(path):
    """Pull the image and its init packet out of the DFU zip."""
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))["manifest"]
        section = manifest.get("application") or manifest.get("softdevice")
        if section is None:
            raise SystemExit("no application image in the package")
        bin_name = section["bin_file"]
        dat_name = section["dat_file"]
        return z.read(bin_name), z.read(dat_name), bin_name


async def find(name, timeout):
    print(f"  scanning for {name} ...")
    dev = await BleakScanner.find_device_by_name(name, timeout=timeout)
    return dev


async def enter_ota_mode():
    """Ask a running firmware to reboot into the bootloader's BLE DFU mode."""
    dev = await find(APP_NAME, 15)
    if dev is None:
        return False
    print(f"  found {dev.address}, requesting OTA reboot")
    async with BleakClient(dev, timeout=30) as c:
        # 0x0F is the DFU opcode; 0xA5 selects Bluetooth rather than USB, and
        # is deliberately awkward so a stray write cannot reboot the device.
        await c.write_gatt_char(CTRL_UUID, bytes([0x0F, 0xA5]), response=False)
    return True


async def clear_stale_state(dev):
    """Reset a bootloader left mid-transfer by an aborted update.

    A failed attempt leaves the DFU state machine part-way through, and every
    subsequent START_DFU is answered with INVALID_STATE until something
    resets it. Sending the reset opcode costs one reboot and saves needing a
    human with a finger on the button.
    """
    try:
        async with BleakClient(dev, timeout=25) as c:
            await c.write_gatt_char(DFU_CONTROL, bytes([SYSTEM_RESET]),
                                    response=False)
    except Exception:
        pass          # the board resets mid-write; the disconnect is expected


async def upload(dev, image, init, verbose):
    events = asyncio.Queue()

    def on_notify(_h, data):
        b = bytes(data)
        if verbose:
            print(f"    [notify] {b.hex()}")
        events.put_nowait(b)

    async def expect(opcode, what):
        while True:
            msg = await asyncio.wait_for(events.get(), timeout=40)
            if msg and msg[0] == PKT_RECEIPT:
                continue                      # flow control, not a response
            if len(msg) >= 3 and msg[0] == RESPONSE and msg[1] == opcode:
                if msg[2] != 0x01:
                    raise SystemExit(f"{what} failed, status {msg[2]}")
                return
            raise SystemExit(f"unexpected reply to {what}: {msg.hex()}")

    async with BleakClient(dev, timeout=40) as c:
        await c.start_notify(DFU_CONTROL, on_notify)

        # 1. Announce an application update and give the three image sizes.
        await c.write_gatt_char(DFU_CONTROL, bytes([START_DFU, UPDATE_APP]),
                                response=True)
        await c.write_gatt_char(DFU_PACKET,
                                struct.pack("<III", 0, 0, len(image)),
                                response=False)
        await expect(START_DFU, "start")

        # 2. The init packet carries the device/version checks.
        await c.write_gatt_char(DFU_CONTROL, bytes([INITIALIZE_DFU, 0x00]),
                                response=True)
        await c.write_gatt_char(DFU_PACKET, init, response=False)
        await c.write_gatt_char(DFU_CONTROL, bytes([INITIALIZE_DFU, 0x01]),
                                response=True)
        await expect(INITIALIZE_DFU, "init packet")

        # 3. Ask for periodic receipts. Without flow control a write-without-
        #    response stream outruns the bootloader's flash writes.
        if RECEIPT_EVERY:
            await c.write_gatt_char(
                DFU_CONTROL,
                bytes([PKT_RECEIPT_REQ]) + struct.pack("<H", RECEIPT_EVERY),
                response=True)
            if verbose:
                print("    [sent] packet-receipt request")

        await c.write_gatt_char(DFU_CONTROL, bytes([RECEIVE_FIRMWARE]),
                                response=True)
        # The bootloader erases the target bank when this arrives. That takes
        # a while on 245 KB, and writing into the erase produced an immediate
        # OPERATION_FAILED, so give it room before the first packet.
        # Single-bank DFU erases the whole application region before it can
        # take data: 245 KB is about sixty sectors, and at roughly 40 ms each
        # that is well over two seconds. Writing into the erase failed on the
        # very first packet.
        await asyncio.sleep(ERASE_WAIT)

        total = len(image)
        sent = 0
        since_receipt = 0
        pct = -1
        while sent < total:
            piece = image[sent:sent + CHUNK]
            await c.write_gatt_char(DFU_PACKET, piece, response=True)
            sent += len(piece)
            since_receipt += 1
            if not RECEIPT_EVERY and PACE_S:
                await asyncio.sleep(PACE_S)

            if RECEIPT_EVERY and since_receipt == RECEIPT_EVERY:
                since_receipt = 0
                msg = await asyncio.wait_for(events.get(), timeout=40)
                if not msg or msg[0] != PKT_RECEIPT:
                    if len(msg) >= 3 and msg[0] == RESPONSE:
                        codes = {1: "success", 2: "invalid state",
                                 3: "not supported", 4: "size exceeds limit",
                                 5: "crc error", 6: "operation failed"}
                        raise SystemExit(
                            f"bootloader rejected the transfer at {sent} B: "
                            f"opcode {msg[1]:#04x} status {msg[2]:#04x} "
                            f"({codes.get(msg[2], '?')})")
                    raise SystemExit(f"expected a receipt, got {msg.hex()}")
                got = struct.unpack("<I", msg[1:5])[0]
                if got != sent:
                    raise SystemExit(f"bootloader has {got} B, host sent {sent}")

            if not events.empty():
                msg = events.get_nowait()
                if len(msg) >= 3 and msg[0] == RESPONSE and msg[2] != 0x01:
                    raise SystemExit(
                        f"bootloader rejected the transfer at {sent} B: "
                        f"opcode {msg[1]:#04x} status {msg[2]:#04x}")

            now = sent * 100 // total
            if now != pct and (verbose or now % 5 == 0):
                pct = now
                print(f"\r  uploading {now:3d}%  ({sent}/{total} B)",
                      end="", flush=True)
        print()

        await expect(RECEIVE_FIRMWARE, "image transfer")

        await c.write_gatt_char(DFU_CONTROL, bytes([VALIDATE]), response=True)
        await expect(VALIDATE, "validate")

        print("  activating ...")
        # The board resets on this one, so the write is not acknowledged.
        try:
            await c.write_gatt_char(DFU_CONTROL, bytes([ACTIVATE_RESET]),
                                    response=False)
        except Exception:
            pass


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("package", help="DFU zip from adafruit-nrfutil genpkg")
    ap.add_argument("--already-in-dfu", action="store_true",
                    help="skip asking the app to reboot")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    image, init, name = load_package(args.package)
    if len(image) % 4:
        # The bootloader rejects any packet whose length is not a multiple of
        # four, which includes the final short one.
        pad = 4 - (len(image) % 4)
        image += b"\xff" * pad
        print(f"  padded image by {pad} B to a word boundary")
    print(f"  package: {name}  {len(image)} B image, {len(init)} B init")

    if not args.already_in_dfu:
        if not await enter_ota_mode():
            print("  no running firmware found; assuming already in DFU mode")
        await asyncio.sleep(3)

    dev = await find(DFU_NAME, 25)
    if dev is None:
        raise SystemExit(f"{DFU_NAME} not found. Is the board in BLE DFU mode?")
    print(f"  found {dev.address}")

    try:
        await upload(dev, image, init, args.verbose)
    except SystemExit as e:
        if "status 2" not in str(e):
            raise
        # INVALID_STATE: a previous attempt left the bootloader part-way in.
        print("  bootloader in a stale state; resetting and retrying once")
        await clear_stale_state(dev)
        await asyncio.sleep(6)
        if not args.already_in_dfu:
            await enter_ota_mode()
            await asyncio.sleep(3)
        dev = await find(DFU_NAME, 30)
        if dev is None:
            raise SystemExit("board did not come back in DFU mode")
        await upload(dev, image, init, args.verbose)
    print("  done — the board is rebooting into the new firmware")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
