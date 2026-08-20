#!/usr/bin/env python3
"""
Reference relay: hold the Bluetooth link to the board, forward frames to a
Boswell server, and apply control commands it sends back.

This is the shape the phone app takes. It decodes nothing and stores nothing —
frames go up exactly as the board emitted them, and control writes come down
as two opaque bytes. Everything that needs a GPU stays on the server.

    uv run host/relay.py --server ws://10.0.0.19:8000 --token $BOSWELL_TOKEN
"""

import argparse
import asyncio
import json
import sys

import websockets
from bleak import BleakClient, BleakScanner

from ble_capture import AUDIO_UUID, CTRL_UUID, INFO_UUID, DEVICE_NAME


async def relay(args):
    print(f"scanning for {DEVICE_NAME} ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if dev is None:
        print(f"{DEVICE_NAME} not found", file=sys.stderr)
        return 1

    url = args.server.rstrip("/") + "/ingest"
    if args.token:
        url += f"?token={args.token}"

    async with BleakClient(dev, timeout=30.0) as board:
        print(f"board connected ({dev.address})")
        async with websockets.connect(url, max_queue=None) as server:
            print(f"server connected ({args.server})")
            loop = asyncio.get_running_loop()
            outbox: asyncio.Queue = asyncio.Queue(maxsize=512)

            dropped = [0]

            def _enqueue(frame: bytes):
                # Runs on the event loop, which is where put_nowait can raise.
                try:
                    outbox.put_nowait(frame)
                except asyncio.QueueFull:
                    dropped[0] += 1

            def on_frame(_sender, data: bytearray):
                # Called from the BLE thread; hand off without blocking it.
                #
                # The except clause used to sit around call_soon_threadsafe,
                # which only schedules the put -- so QueueFull was raised
                # later, on the event loop, outside any handler. Drops printed
                # a traceback from asyncio instead of being counted, and the
                # count that was supposed to exist never incremented.
                try:
                    loop.call_soon_threadsafe(_enqueue, bytes(data))
                except RuntimeError:
                    dropped[0] += 1      # loop already closed

            await board.start_notify(AUDIO_UUID, on_frame)
            await board.write_gatt_char(CTRL_UUID, bytes([0x01, 1]), response=True)

            async def pump_up():
                sent = 0
                while True:
                    await server.send(await outbox.get())
                    sent += 1
                    if sent % 250 == 0:
                        print(f"  relayed {sent} frames")

            async def pump_down():
                """Control commands from the server, written straight through."""
                async for msg in server:
                    if isinstance(msg, bytes):
                        continue
                    m = json.loads(msg)
                    if m.get("type") == "ctrl":
                        await board.write_gatt_char(
                            CTRL_UUID, bytes([m["op"], m["arg"]]), response=True)
                        print(f"  ctrl -> op=0x{m['op']:02X} arg={m['arg']}")

            async def status():
                while True:
                    info = await board.read_gatt_char(INFO_UUID)
                    msg = {"type": "status"}
                    if len(info) >= 6:
                        msg["rate"] = 16000 if info[1] else 8000
                    if len(info) >= 8:
                        msg["imu"] = info[6] != 0
                    if len(info) >= 32:
                        pend = info[28] | (info[29] << 8) | (info[30] << 16)
                        msg["backlog_bytes"] = pend
                        msg["backlog_seconds"] = round(pend / 4500.0, 1)
                        msg["qspi_mb"] = round(info[31] * 65536 / 1048576)
                    if len(info) >= 22:
                        # Whether the device is actually capturing, and which
                        # firmware said so. Relayed status reported the rate,
                        # the IMU and the backlog but never this, so a relay
                        # could show a healthy link beside a device that was
                        # not recording. Only read where the firmware declares
                        # the bit means something (INFO_CAP_STATE, 0x0040).
                        caps = info[20] | (info[21] << 8)
                        msg["firmware"] = {1: "arduino", 2: "zephyr"}.get(info[19])
                        msg["device_streaming"] = (
                            bool(info[5] & 4) if caps & 0x0040 else None)
                    if dropped[0]:
                        msg["relay_dropped"] = dropped[0]
                    await server.send(json.dumps(msg))
                    await asyncio.sleep(2)

            done, pending = await asyncio.wait(
                [asyncio.create_task(pump_up()),
                 asyncio.create_task(pump_down()),
                 asyncio.create_task(status())],
                return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            for t in done:
                if t.exception():
                    print("relay stopped:", t.exception(), file=sys.stderr)
    return 0


ap = argparse.ArgumentParser()
ap.add_argument("--server", default="ws://localhost:8000")
ap.add_argument("--token", default="")
sys.exit(asyncio.run(relay(ap.parse_args())))
