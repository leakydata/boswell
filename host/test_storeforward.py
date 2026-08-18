#!/usr/bin/env python3
"""
Verify store-and-forward: arm capture, drop the link, let the device buffer to
flash, reconnect, and confirm the backlog arrives before live audio resumes.

    uv run host/test_storeforward.py --offline 20
"""
import argparse, asyncio, os, struct, sys, time
import numpy as np, soundfile as sf
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ble_capture import (AUDIO_UUID, CTRL_UUID, INFO_UUID, DEVICE_NAME,
                         HEADER_LEN, decode_block)
from bleak import BleakClient, BleakScanner


def qspi(info):
    if len(info) < 32:
        return None
    return info[27], info[28] | (info[29] << 8) | (info[30] << 16)


async def main(a):
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if not dev:
        print("XIAO-MIC not found"); return 1

    print("1. connecting and arming capture")
    async with BleakClient(dev, timeout=30.0) as c:
        ok, pend = qspi(await c.read_gatt_char(INFO_UUID))
        print(f"   QSPI ready={bool(ok)} backlog={pend} B")
        await c.write_gatt_char(CTRL_UUID, bytes([0x01, 1]), response=True)
        await asyncio.sleep(3)
        print("2. disconnecting — device should keep capturing to flash")

    print(f"3. offline for {a.offline}s (talk now if you like)")
    await asyncio.sleep(a.offline)

    print("4. reconnecting")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if not dev:
        print("not advertising after disconnect"); return 1

    frames, t_first, t_backlog_done = [], None, None
    def on_audio(_s, data):
        nonlocal t_first
        if len(data) < HEADER_LEN: return
        _q, _f, idx, pred, ns = struct.unpack("<HBBhH", data[:HEADER_LEN])
        p = data[HEADER_LEN:]
        if len(p) >= ns // 2:
            if t_first is None: t_first = time.time()
            frames.append(decode_block(p, pred, idx, ns))

    async with BleakClient(dev, timeout=30.0) as c:
        info = await c.read_gatt_char(INFO_UUID)
        ok, pend = qspi(info)
        print(f"   backlog on reconnect: {pend} B  (~{pend/4500:.1f}s of audio)")
        if pend == 0:
            print("   ! nothing buffered — store-and-forward did not engage")
        await c.start_notify(AUDIO_UUID, on_audio)
        t0 = time.time()
        while time.time() - t0 < a.drain:
            await asyncio.sleep(0.5)
            _ok, p = qspi(await c.read_gatt_char(INFO_UUID))
            if p == 0 and t_backlog_done is None and t_first is not None:
                t_backlog_done = time.time()
                print(f"   backlog drained in {t_backlog_done - t0:.1f}s")
        await c.write_gatt_char(CTRL_UUID, bytes([0x01, 0]), response=True)

    if not frames:
        print("no audio received"); return 1
    audio = np.concatenate(frames)
    sf.write(a.out, audio, 8000, subtype="PCM_16")
    secs = len(audio) / 8000
    print(f"\nwrote {a.out}")
    print(f"  received {secs:.1f}s of audio in {a.drain:.0f}s of wall clock")
    print(f"  recovered {secs - a.drain:.1f}s more than realtime"
          if secs > a.drain else "  no backlog recovered")
    return 0


ap = argparse.ArgumentParser()
ap.add_argument("--offline", type=float, default=20)
ap.add_argument("--drain", type=float, default=20)
ap.add_argument("--out", default="data/storeforward.wav")
sys.exit(asyncio.run(main(ap.parse_args())))
