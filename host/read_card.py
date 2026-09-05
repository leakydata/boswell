#!/usr/bin/env python3
"""Read a recording the device wrote to its card.

Choosing FAT over raw sectors was a bet that something else could read these
files later. This is that something else, and running it against a real card
is what makes the bet good.

    uv run host/read_card.py /mnt/card/boswell/b0000e9cd_0000.bwl -o out.wav
    uv run host/read_card.py /mnt/card/boswell            # report on all of them

The payload of every record is a whole proto frame, byte for byte as the
radio would have carried it, so decoding is the same code that decodes a live
stream -- which is the point of storing it that way rather than inventing a
second format for the card.
"""

import argparse
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ble_capture import (decode_frame, payload_is_complete,
                         HEADER_LEN as FRAME_HEADER_LEN)

MAGIC = b"BSWL"
HEADER_LEN = 16
VERSION = 1

CODEC_NAMES = {1: "ADPCM", 20: "Opus"}


class BadFile(Exception):
    pass


def read_header(f):
    raw = f.read(HEADER_LEN)
    if len(raw) < HEADER_LEN:
        raise BadFile("shorter than a header")
    if raw[:4] != MAGIC:
        raise BadFile(f"bad magic {raw[:4]!r}")
    version = raw[4]
    if version != VERSION:
        raise BadFile(f"version {version}, expected {VERSION}")
    codec, rate, boot_id = raw[5], *struct.unpack("<HI", raw[6:12])
    return {"version": version, "codec": codec, "rate": rate,
            "boot_id": boot_id,
            "codec_name": CODEC_NAMES.get(codec, f"unknown({codec})")}


def read_frames(f):
    """Yield each frame's payload.

    A file whose tail is torn -- the battery went during a batch -- stops
    here rather than raising. Everything before the tear is real audio and
    is worth keeping; refusing the whole file over its last few milliseconds
    would throw away the recording to protect the recording.
    """
    while True:
        head = f.read(2)
        if len(head) < 2:
            return
        (n,) = struct.unpack("<H", head)
        if n == 0 or n > 4096:
            return                     # not a length; the file is torn here
        payload = f.read(n)
        if len(payload) < n:
            return
        yield payload


def read_file(path, want_audio=True):
    with open(path, "rb") as f:
        hdr = read_header(f)
        frames = 0
        short = 0
        torn = False
        samples = []
        first_ms = last_ms = None
        size = os.path.getsize(path)
        consumed = HEADER_LEN

        for payload in read_frames(f):
            frames += 1
            consumed += 2 + len(payload)
            if len(payload) < FRAME_HEADER_LEN:
                short += 1
                continue

            # The record is a whole proto frame, so it is unpacked exactly as
            # a live one is -- including the flags, which and not the file
            # header decide the codec. A file written across a firmware
            # change carries both, and the frame is the one that knows.
            seq, flags, index, predictor, nsamples, t_ms = struct.unpack(
                "<HBBhHI", payload[:FRAME_HEADER_LEN])
            body = payload[FRAME_HEADER_LEN:]

            if not payload_is_complete(body, flags, nsamples):
                short += 1
                continue

            first_ms = t_ms if first_ms is None else first_ms
            last_ms = t_ms

            if want_audio:
                try:
                    pcm = decode_frame(body, flags, predictor, index, nsamples)
                except Exception:
                    short += 1
                    continue
                if pcm is not None:
                    samples.append(pcm)

        torn = consumed != size

    hdr.update(frames=frames, short=short, bytes=consumed, size=size,
               torn=torn, samples=samples,
               first_ms=first_ms, last_ms=last_ms)
    return hdr


def describe(path, hdr):
    secs = hdr["frames"] * 20 / 1000.0
    bits = [f"{os.path.basename(path)}", f"{hdr['codec_name']} {hdr['rate']} Hz",
            f"boot={hdr['boot_id']:04x}",
            f"{hdr['frames']} frames", f"{secs:.1f}s"]
    if hdr["first_ms"] is not None:
        # Device uptime, not wall clock -- there is no clock on the device
        # yet, which is what phase 03 is for.
        span = (hdr["last_ms"] - hdr["first_ms"]) / 1000.0
        bits.append(f"device {hdr['first_ms'] / 1000:.0f}-"
                    f"{hdr['last_ms'] / 1000:.0f}s ({span:.0f}s span)")
    if hdr["short"]:
        bits.append(f"{hdr['short']} unusable")
    if hdr["torn"]:
        bits.append(f"torn tail, {hdr['size'] - hdr['bytes']} bytes ignored")
    return "  ".join(bits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a .bwl file, or a directory of them")
    ap.add_argument("-o", "--out", help="write decoded audio to this WAV")
    args = ap.parse_args()

    paths = []
    if os.path.isdir(args.path):
        paths = sorted(os.path.join(args.path, n)
                       for n in os.listdir(args.path) if n.endswith(".bwl"))
    else:
        paths = [args.path]

    if not paths:
        print("no .bwl files there", file=sys.stderr)
        return 1

    all_pcm = []
    rate = None
    bad = 0

    for p in paths:
        try:
            hdr = read_file(p, want_audio=bool(args.out))
        except BadFile as e:
            print(f"{os.path.basename(p)}: {e}")
            bad += 1
            continue
        print(describe(p, hdr))
        if args.out:
            rate = hdr["rate"]
            all_pcm.extend(hdr["samples"])

    if args.out and all_pcm:
        import numpy as np
        import soundfile as sf
        sf.write(args.out, np.concatenate(all_pcm), rate)
        print(f"wrote {args.out}")

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
