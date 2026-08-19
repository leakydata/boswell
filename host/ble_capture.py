#!/usr/bin/env python3
"""
Phase 2 host receiver: connect to XIAO-MIC over BLE, decode ADPCM, write WAV.

Usage:
    uv run host/ble_capture.py --scan
    uv run host/ble_capture.py --seconds 30 --out data/ble_voice.wav
    uv run host/ble_capture.py --seconds 30 --rate16       # needs a BT5 adapter
"""

import argparse
import asyncio
import struct
import sys
import time

import numpy as np
import soundfile as sf
from bleak import BleakClient, BleakScanner

SERVICE_UUID = "4b1a0001-8f2c-4d5e-9a3b-1c7e6f8d0a21"
AUDIO_UUID   = "4b1a0002-8f2c-4d5e-9a3b-1c7e6f8d0a21"
CTRL_UUID    = "4b1a0003-8f2c-4d5e-9a3b-1c7e6f8d0a21"
INFO_UUID    = "4b1a0004-8f2c-4d5e-9a3b-1c7e6f8d0a21"

DEVICE_NAME = "XIAO-MIC"
HEADER_LEN = 12   # seq,flags,state,nsamples,t_ms

INDEX_TABLE = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]

STEP_TABLE = [
        7,     8,     9,    10,    11,    12,    13,    14,    16,    17,
       19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
       50,    55,    60,    66,    73,    80,    88,    97,   107,   118,
      130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
      337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
      876,   963,  1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
     2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
     5894,  6484,  7132,  7845,  8630,  9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
]


def decode_block(nibbles, predictor, index, nsamples):
    """Mirror of adpcm_encode_block in the firmware. State comes from the
    frame header, so a lost frame cannot desynchronise the ones after it."""
    out = np.empty(nsamples, dtype=np.int16)
    for i in range(nsamples):
        byte = nibbles[i >> 1]
        code = (byte & 0x0F) if (i & 1) == 0 else (byte >> 4)

        step = STEP_TABLE[index]
        diffq = step >> 3
        if code & 4:
            diffq += step
        if code & 2:
            diffq += step >> 1
        if code & 1:
            diffq += step >> 2

        if code & 8:
            predictor -= diffq
        else:
            predictor += diffq

        predictor = max(-32768, min(32767, predictor))
        index += INDEX_TABLE[code]
        index = max(0, min(88, index))
        out[i] = predictor

    return out


async def do_scan():
    print("scanning 8s ...")
    devices = await BleakScanner.discover(timeout=8.0)
    if not devices:
        print("no BLE devices found")
        return
    for d in devices:
        mark = "  <-- this one" if (d.name or "") == DEVICE_NAME else ""
        print(f"  {d.address}  rssi={getattr(d, 'rssi', '?'):>4}  {d.name or '(unnamed)'}{mark}")


async def capture(args):
    print(f"scanning for {DEVICE_NAME} ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if dev is None:
        print(f"{DEVICE_NAME} not found. Is the board powered and advertising?",
              file=sys.stderr)
        return 1

    print(f"found {dev.address}, connecting ...")

    frames = []
    stats = {"frames": 0, "gaps": 0, "bytes": 0, "last_seq": None, "vad_skipped": 0}

    def on_audio(_sender, data: bytearray):
        if len(data) < HEADER_LEN:
            return
        seq, flags, index, predictor, nsamples, t_ms = struct.unpack(
            "<HBBhHI", data[:HEADER_LEN])

        payload = data[HEADER_LEN:]
        if len(payload) < nsamples // 2:
            return

        if stats["last_seq"] is not None:
            gap = (seq - stats["last_seq"] - 1) & 0xFFFF
            if gap:
                # With VAD on, gaps are intentional silence, not packet loss.
                if flags & 0x04:
                    stats["vad_skipped"] += gap
                else:
                    stats["gaps"] += gap
        stats["last_seq"] = seq

        frames.append(decode_block(payload, predictor, index, nsamples))
        stats["frames"] += 1
        stats["bytes"] += len(data)

    async with BleakClient(dev, timeout=30.0) as client:
        print(f"connected (mtu={client.mtu_size})")

        info = await client.read_gatt_char(INFO_UUID)
        codec, is16k, frame_ms, ns_lo, ns_hi, vad = info[:6]
        print(f"  codec={codec} rate={'16k' if is16k else '8k'} "
              f"frame={frame_ms}ms samples={ns_lo | (ns_hi << 8)} vad={vad}")
        if len(info) >= 8:
            bus, addr = info[6], info[7]
            where = {0: "NOT FOUND", 1: "Wire1 (17/16)", 2: "Wire (4/5)"}.get(bus, "?")
            print(f"  IMU: {where}" + (f" addr=0x{addr:02X}" if bus else ""))
            if len(info) >= 32:
                ok = info[27]
                pend = info[28] | (info[29] << 8) | (info[30] << 16)
                mb = info[31] * 65536 / 1048576.0
                if ok:
                    secs = pend / 4500.0     # ~90 B/frame, 50 frames/s
                    print(f"  QSPI: ready, {mb:.0f} MB · backlog {pend} B "
                          f"(~{secs:.1f}s of audio)")
                else:
                    print("  QSPI: NOT FOUND")
            if len(info) >= 13:
                p = info[8:12]
                print(f"    WHO_AM_I probe  bus1@6A=0x{p[0]:02X} bus1@6B=0x{p[1]:02X} "
                      f"bus2@6A=0x{p[2]:02X} bus2@6B=0x{p[3]:02X}")
                print(f"    (0xFF = no ACK / bus idle, 0x00 = stuck low, "
                      f"0x69/0x6A = IMU)   pwr_pol={info[12]}")

        if args.rate16:
            await client.write_gatt_char(CTRL_UUID, bytes([0x02, 1]), response=True)
            print("  requested 16 kHz")
        if args.gain is not None:
            await client.write_gatt_char(CTRL_UUID, bytes([0x03, args.gain]), response=True)
            print(f"  gain -> {args.gain}")
        if args.vad_threshold is not None:
            await client.write_gatt_char(
                CTRL_UUID, bytes([0x05, max(1, min(255, args.vad_threshold))]),
                response=True)
            print(f"  VAD threshold -> {args.vad_threshold * 32}")
        if args.vad:
            await client.write_gatt_char(CTRL_UUID, bytes([0x04, 1]), response=True)
            print("  VAD gating on")

        await client.start_notify(AUDIO_UUID, on_audio)
        await client.write_gatt_char(CTRL_UUID, bytes([0x01, 1]), response=True)

        print(f"streaming for {args.seconds:.0f}s ...")
        start = time.time()
        await asyncio.sleep(args.seconds)
        elapsed = time.time() - start

        await client.write_gatt_char(CTRL_UUID, bytes([0x01, 0]), response=True)
        await client.stop_notify(AUDIO_UUID)

    if not frames:
        print("no audio received", file=sys.stderr)
        return 1

    rate = 16000 if args.rate16 else 8000
    audio = np.concatenate(frames)
    sf.write(args.out, audio, rate, subtype="PCM_16")

    kbps = stats["bytes"] * 8 / elapsed / 1000
    expected = stats["frames"] + stats["gaps"]
    loss = 100.0 * stats["gaps"] / expected if expected else 0.0
    peak = int(np.abs(audio).max())

    print(f"\nwrote {args.out}")
    print(f"  duration     {len(audio) / rate:.2f}s audio in {elapsed:.2f}s wall")
    print(f"  sample rate  {rate} Hz")
    print(f"  frames       {stats['frames']}")
    print(f"  lost frames  {stats['gaps']}  ({loss:.2f}%)")
    if stats["vad_skipped"]:
        print(f"  vad skipped  {stats['vad_skipped']} frames of silence")
    print(f"  throughput   {kbps:.1f} kbps over the air")
    print(f"  peak         {peak} / 32767  ({100 * peak / 32767:.1f}%)")

    # Frames the board accounted for, including ones VAD deliberately dropped.
    # Without this, gating looks identical to a link that cannot keep up.
    accounted = stats["frames"] + stats["gaps"] + stats["vad_skipped"]
    ratio = accounted * 0.020 / elapsed
    if ratio < 0.95:
        print(f"\n  ! only {ratio:.0%} of realtime accounted for — the link is behind.")
        print("    Try 8 kHz, or wait for the BT5 adapter.")
    else:
        print(f"\n  link is keeping up ({ratio:.0%} of realtime accounted for).")
        if stats["vad_skipped"]:
            gated = 100.0 * stats["vad_skipped"] / accounted
            print(f"  VAD gated {gated:.1f}% of airtime.")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="list BLE devices and exit")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default="data/ble_voice.wav")
    ap.add_argument("--rate16", action="store_true", help="16 kHz (needs BT5)")
    ap.add_argument("--gain", type=int, default=None)
    ap.add_argument("--vad", action="store_true")
    ap.add_argument("--vad-threshold", type=int, default=None,
                    help="control units; threshold = n * 32 (see analyze_vad.py)")
    args = ap.parse_args()

    if args.scan:
        asyncio.run(do_scan())
        return 0
    return asyncio.run(capture(args))


if __name__ == "__main__":
    sys.exit(main())
