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

from ble_capture import (decode_frame, payload_is_complete, FLAG_PRE_BOOT,
                         HEADER_LEN as FRAME_HEADER_LEN)

MAGIC = b"BSWL"
# Version 3 grew the header to carry which recorder wrote the file. Earlier
# files are still read at their own length -- refusing them to gain a field
# they were never written with would strand real recordings.
HEADER_LEN = 16
HEADER_LEN_V3 = 24
SUPPORTED_VERSIONS = (1, 2, 3)   # 2 added a checksum, 3 the device id

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
    if version not in SUPPORTED_VERSIONS:
        raise BadFile(f"version {version}, understand {SUPPORTED_VERSIONS}")
    codec, rate, boot_id, boot_epoch = raw[5], *struct.unpack("<HII", raw[6:16])

    device_id = None
    if version >= 3:
        tail = f.read(HEADER_LEN_V3 - HEADER_LEN)
        if len(tail) < HEADER_LEN_V3 - HEADER_LEN:
            raise BadFile("header claims version 3 and stops short")
        addr = tail[:6]
        # All zeroes means the device did not know its own identity when the
        # file was opened -- Bluetooth had not started yet. Unattributed, not
        # a name, and dedup treats the two very differently.
        #
        # Reversed, because a Bluetooth address is little-endian on the wire
        # and big-endian when anybody writes it down. The firmware stores the
        # six bytes as the stack hands them over; a host that connected to the
        # same device calls it D9:66:CF:BB:58:A4. Without this the card path
        # and the radio path give one recorder two different names, and
        # de-duplication fails in exactly the case it exists for -- which is
        # what the first real v3 file showed: a458bbcf66d9 on the card against
        # d966cfbb58a4 over the air.
        if any(addr):
            device_id = addr[::-1].hex()

    return {"version": version, "codec": codec, "rate": rate,
            "boot_id": boot_id,
            # Lower-case hex without separators, which is what _norm_addr in
            # ble_capture produces from whatever a host calls the address.
            "device_id": device_id,
            # The wall-clock second that was uptime zero, or 0 if no host had
            # told the device the time before this file opened. Any frame can
            # be placed from this plus its own device_ms.
            "boot_epoch": boot_epoch,
            "codec_name": CODEC_NAMES.get(codec, f"unknown({codec})")}


# The same CRC the device writes, and the same one its flash backlog already
# used: polynomial 0x07, init 0xFF. Table-driven because this runs once per
# record and a card holds hundreds of thousands of them -- the bit-at-a-time
# version was two seconds of every file.
_CRC8 = []
for _b in range(256):
    _c = _b
    for _ in range(8):
        _c = ((_c << 1) ^ 0x07) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _CRC8.append(_c)


def crc8(data):
    crc = 0xFF
    for b in data:
        crc = _CRC8[crc ^ b]
    return crc


def read_frames(f, version):
    """Yield (payload, ok) for each record.

    Reads the whole file into memory first and walks it there. The obvious
    version -- three small reads per record, for the length, the checksum and
    the payload -- is thirty thousand reads for one recording, and against a
    card mounted over USB that was ten of the thirteen seconds each file
    took. Decoding the audio was half a second of it. A recording is about a
    megabyte; holding one is nothing, and it turns the slowest part of an
    import into the fastest.

    A file whose tail is torn -- the battery went during a batch -- stops
    here rather than raising. Everything before the tear is real audio and is
    worth keeping; refusing the whole file over its last few milliseconds
    would throw away the recording to protect the recording.

    A record whose checksum fails is yielded with ok=False rather than
    ending the read. The length chain is still intact after it, so the rest
    of the file is still readable, and one scrambled frame is 20 ms.
    """
    buf = f.read()
    pos = 0
    end = len(buf)
    extra = 1 if version >= 2 else 0

    while True:
        if pos + 2 + extra > end:
            return
        n = buf[pos] | (buf[pos + 1] << 8)
        if n == 0 or n > 4096:
            return                     # not a length; the file is torn here

        want_crc = buf[pos + 2] if extra else None
        start = pos + 2 + extra
        if start + n > end:
            return                     # the tail is torn

        payload = buf[start:start + n]
        pos = start + n

        ok = want_crc is None or crc8(payload) == want_crc
        yield payload, ok


def read_file(path, want_audio=True):
    with open(path, "rb") as f:
        hdr = read_header(f)
        frames = 0
        short = 0
        torn = False
        samples = []
        first_ms = last_ms = None
        pre_boot = 0
        corrupt = 0
        frame_ms = []
        size = os.path.getsize(path)
        consumed = HEADER_LEN_V3 if hdr["version"] >= 3 else HEADER_LEN

        for payload, ok in read_frames(f, hdr["version"]):
            frames += 1
            consumed += 2 + len(payload) + (1 if hdr["version"] >= 2 else 0)

            if not ok:
                # The bytes are not what was written. Decoding them would
                # produce noise presented as audio, which is worse than a
                # gap: a gap is visible.
                corrupt += 1
                continue
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

            if flags & FLAG_PRE_BOOT:
                pre_boot += 1
            else:
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
                    frame_ms.append(t_ms)

        torn = consumed != size

    hdr.update(frames=frames, short=short, bytes=consumed, size=size,
               torn=torn, samples=samples, pre_boot=pre_boot,
               corrupt=corrupt, frame_ms=frame_ms,
               first_ms=first_ms, last_ms=last_ms)
    return hdr


def describe(path, hdr):
    secs = hdr["frames"] * 20 / 1000.0
    bits = [f"{os.path.basename(path)}", f"{hdr['codec_name']} {hdr['rate']} Hz",
            f"boot={hdr['boot_id']:04x}" if hdr["boot_id"]
            else "boot=unknown (pre-boot audio)",
            f"{hdr['frames']} frames", f"{secs:.1f}s"]
    if hdr.get("device_id"):
        bits.append("dev " + hdr["device_id"][-4:])
    if hdr.get("boot_epoch"):
        import datetime
        when = datetime.datetime.fromtimestamp(
            hdr["boot_epoch"] + (hdr["first_ms"] or 0) / 1000.0)
        bits.append(when.strftime("%Y-%m-%d %H:%M"))
    else:
        bits.append("no clock")
    if hdr["first_ms"] is not None:
        # Device uptime, not wall clock -- there is no clock on the device
        # yet, which is what phase 03 is for.
        span = (hdr["last_ms"] - hdr["first_ms"]) / 1000.0
        bits.append(f"device {hdr['first_ms'] / 1000:.0f}-"
                    f"{hdr['last_ms'] / 1000:.0f}s ({span:.0f}s span)")
    if hdr["pre_boot"]:
        # These carry a previous run's uptime. Their times cannot be compared
        # with anything else in the file, and the device says so rather than
        # letting them be read as though they belonged here.
        bits.append(f"{hdr['pre_boot']} from an earlier boot")
    if hdr["corrupt"]:
        bits.append(f"{hdr['corrupt']} FAILED CHECKSUM")
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
