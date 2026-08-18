#!/usr/bin/env python3
"""
Sweep PDM gain over BLE and report levels at each setting.

nRF52840 PDM gain register: 0x00 = -20 dB, 0x28 (40) = 0 dB, 0x50 (80) = +20 dB,
0.5 dB per step. Gain is changed live over the control characteristic, so this
needs no reflash.

Run it once in a quiet room to get the noise floor, then again while speaking
to get speech levels. The gap between the two is the SNR the VAD has to work
with.

Usage:
    uv run host/tune_gain.py --seconds 4
    uv run host/tune_gain.py --seconds 6 --gains 40,50,60,70
"""

import argparse
import asyncio
import os
import struct
import sys

import numpy as np
import soundfile as sf
from bleak import BleakClient, BleakScanner

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ble_capture import (AUDIO_UUID, CTRL_UUID, DEVICE_NAME, HEADER_LEN,
                         decode_block)


def gain_db(g):
    return (g - 40) * 0.5


async def run(args):
    gains = [int(g) for g in args.gains.split(",")]

    print(f"scanning for {DEVICE_NAME} ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if dev is None:
        print(f"{DEVICE_NAME} not found", file=sys.stderr)
        return 1

    rows = []
    async with BleakClient(dev, timeout=30.0) as client:
        print(f"connected (mtu={client.mtu_size})\n")

        frames = []

        def on_audio(_s, data: bytearray):
            if len(data) < HEADER_LEN:
                return
            _seq, _flags, index, predictor, nsamples = struct.unpack(
                "<HBBhH", data[:HEADER_LEN])
            payload = data[HEADER_LEN:]
            if len(payload) >= nsamples // 2:
                frames.append(decode_block(payload, predictor, index, nsamples))

        await client.start_notify(AUDIO_UUID, on_audio)

        for g in gains:
            await client.write_gatt_char(CTRL_UUID, bytes([0x03, g]), response=True)
            await client.write_gatt_char(CTRL_UUID, bytes([0x01, 1]), response=True)
            await asyncio.sleep(0.6)          # let PDM restart and settle
            frames.clear()
            await asyncio.sleep(args.seconds)
            await client.write_gatt_char(CTRL_UUID, bytes([0x01, 0]), response=True)

            if not frames:
                print(f"  gain {g}: no audio")
                continue

            audio = np.concatenate(frames).astype(np.float64)
            rms = float(np.sqrt(np.mean(audio ** 2)))
            peak = float(np.abs(audio).max())
            clip = int((np.abs(audio) >= 32700).sum())
            # Per-frame RMS shows how much the level moves, which is what the
            # VAD actually keys on -- a flat trace means nothing to gate on.
            fr = audio[: len(audio) // 160 * 160].reshape(-1, 160)
            frms = np.sqrt(np.mean(fr ** 2, axis=1))
            rows.append((g, rms, peak, clip, float(frms.min()), float(frms.max())))

            if args.save:
                sf.write(f"data/gain_{g}.wav",
                         np.concatenate(frames), 8000, subtype="PCM_16")

            print(f"  gain {g:>2} ({gain_db(g):+5.1f} dB)  "
                  f"rms {rms:8.1f}  peak {peak:6.0f} ({100*peak/32767:5.1f}%)  "
                  f"clip {clip:>5}  frameRMS {frms.min():7.1f}..{frms.max():7.1f}")

        await client.stop_notify(AUDIO_UUID)

    if not rows:
        return 1

    print("\n" + "=" * 74)
    print(f"{'gain':>5} {'dB':>7} {'rms':>9} {'peak%':>7} {'clipped':>8}  verdict")
    print("-" * 74)
    for g, rms, peak, clip, _lo, _hi in rows:
        pk = 100 * peak / 32767
        if clip > 0:
            verdict = "CLIPPING - too hot"
        elif pk > 70:
            verdict = "hot, near clipping"
        elif pk > 25:
            verdict = "good headroom"
        elif pk > 8:
            verdict = "usable, quiet"
        else:
            verdict = "too quiet"
        print(f"{g:>5} {gain_db(g):>+6.1f} {rms:>9.1f} {pk:>6.1f}% {clip:>8}  {verdict}")
    print("=" * 74)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--gains", default="20,30,40,50,60,70")
    ap.add_argument("--save", action="store_true", help="write data/gain_N.wav")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
