#!/usr/bin/env python3
"""
Phase 1 host receiver: read framed PCM from the XIAO over USB CDC, write WAV.

Wire format (little-endian), matching firmware/mic_usb_pcm:
    0xA5 0x5A | seq:u16 | drops:u16 | nsamples:u16 | int16 pcm[nsamples]

Usage:
    uv run host/capture_serial.py --seconds 15 --out data/test.wav
"""

import argparse
import struct
import sys
import time

import numpy as np
import serial
import soundfile as sf

MAGIC = b"\xa5\x5a"
HEADER_LEN = 8
SAMPLE_RATE = 16000


def resync(port):
    """Scan byte-by-byte until the magic prefix lands, then return the header."""
    window = b""
    while len(window) < 2:
        window += port.read(1)
    while window[-2:] != MAGIC:
        b = port.read(1)
        if not b:
            return None
        window += b
    return port.read(HEADER_LEN - 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--out", default="data/capture.wav")
    args = ap.parse_args()

    port = serial.Serial(args.port, 115200, timeout=2)
    # The board free-runs, so whatever is already buffered is mid-frame.
    time.sleep(0.2)
    port.reset_input_buffer()

    chunks = []
    total_samples = 0
    frames = 0
    resyncs = 0
    last_seq = None
    seq_gaps = 0
    firmware_drops = 0
    target = int(SAMPLE_RATE * args.seconds)
    start = time.time()

    print(f"capturing {args.seconds:.0f}s from {args.port} ...")

    while total_samples < target:
        if time.time() - start > args.seconds * 3 + 10:
            print("timeout: board is not streaming enough data", file=sys.stderr)
            break

        head = port.read(2)
        if len(head) < 2:
            print("read timeout — is the sketch running?", file=sys.stderr)
            break

        if head != MAGIC:
            rest = resync(port)
            if rest is None:
                break
            resyncs += 1
        else:
            rest = port.read(HEADER_LEN - 2)
            if len(rest) < HEADER_LEN - 2:
                break

        seq, drops, nsamples = struct.unpack("<HHH", rest)

        # A corrupt length would desynchronise the stream; drop and resync.
        if nsamples == 0 or nsamples > 4096:
            resyncs += 1
            continue

        payload = port.read(nsamples * 2)
        if len(payload) < nsamples * 2:
            break

        if last_seq is not None:
            gap = (seq - last_seq - 1) & 0xFFFF
            if gap:
                seq_gaps += gap
        last_seq = seq
        firmware_drops = drops

        chunks.append(np.frombuffer(payload, dtype="<i2"))
        total_samples += nsamples
        frames += 1

    port.close()

    if not chunks:
        print("no audio captured", file=sys.stderr)
        return 1

    audio = np.concatenate(chunks)
    sf.write(args.out, audio, SAMPLE_RATE, subtype="PCM_16")

    elapsed = time.time() - start
    peak = int(np.abs(audio).max())
    rms = float(np.sqrt(np.mean((audio.astype(np.float64)) ** 2)))
    clipped = int((np.abs(audio) >= 32700).sum())

    print(f"\nwrote {args.out}")
    print(f"  duration       {len(audio) / SAMPLE_RATE:.2f}s in {elapsed:.2f}s wall")
    print(f"  frames         {frames}")
    print(f"  peak           {peak} / 32767  ({100 * peak / 32767:.1f}% full scale)")
    print(f"  rms            {rms:.1f}")
    print(f"  clipped        {clipped} samples")
    print(f"  usb resyncs    {resyncs}")
    print(f"  seq gaps       {seq_gaps}   (lost frames on the USB link)")
    print(f"  firmware drops {firmware_drops}   (host too slow / board overran)")

    if peak < 500:
        print("\n  ! near-silent. Raise MIC_GAIN in the sketch, or the mic is not running.")
    elif clipped > len(audio) * 0.001:
        print("\n  ! clipping. Lower MIC_GAIN in the sketch.")
    else:
        print("\n  levels look healthy.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
