#!/usr/bin/env python3
"""Record from an Omi device into the Boswell archive.

    uv run host/omi_capture.py --scan
    uv run host/omi_capture.py --address C4:B3:FD:7F:1E:91

An Omi is a second recorder, not a second Boswell. It streams Opus over
Bluetooth with the same encoder settings this project uses -- 20 ms frames,
16 kHz, 32 kbps, CELT restricted low delay -- so the audio needs no
translation at all. What it does not have is Boswell's frame header, and the
difference is the whole of this file.

    Boswell   [seq:u16][flags][index][predictor:i16][nsamples:u16][t_ms:u32]
    Omi       [packet:u16][index:u8]

Two things are missing from that, and each needs a decision rather than a
default.

**There is no timestamp.** Boswell frames carry the device's own uptime, and
de-duplication keys on it. An Omi carries a packet counter, so the counter is
what stands in: packets are 20 ms apart by construction, and packet x 20 ms
is a monotonic device clock in everything but name.

**There is no boot id.** The counter restarts from zero when the device does,
and without something to say which run a count belongs to, two sessions look
like the same audio. A run is identified here instead: a new one is declared
when the counter goes backwards, and it gets a random id of its own -- which
is exactly what the Boswell firmware does, one layer up.

A BLE peripheral takes one connection at a time, so while this is running the
Omi phone app cannot have the device, and vice versa.
"""

import argparse
import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))

import numpy as np
from bleak import BleakClient, BleakScanner

from ble_capture import decode_opus
import clipwriter

OMI_SERVICE = "19b10000-e8f2-537e-4f6c-d104768a1214"
OMI_AUDIO   = "19b10001-e8f2-537e-4f6c-d104768a1214"
OMI_CODEC   = "19b10002-e8f2-537e-4f6c-d104768a1214"

# Their numbering, which is by frame length rather than by codec: 20 is Opus
# at 10 ms (the devkit), 21 is Opus at 20 ms (the CV 1). Boswell's own
# PROTO_CODEC_OPUS is also 20 and means 20 ms -- the collision is in the
# names, not on the wire, because these are different protocols.
CODEC_FRAMES = {20: 160, 21: 320}

HEADER_LEN = 3          # packet:u16, index:u8
RATE = 16000
FRAME_MS = 20
CLIP_SECONDS = 30


def norm_id(addr):
    """The archive's name for a recorder: lower-case hex, no separators."""
    return "".join(c for c in str(addr or "").lower()
                   if c in "0123456789abcdef") or None


class Run:
    """One continuous stretch of a device's counter.

    The counter restarting is the only evidence available that the device
    rebooted, so it is what defines a run. Each gets an id of its own, and
    that id is what makes the packet counter usable as an identity -- the
    same job boot_id does for a Boswell.
    """

    def __init__(self):
        self.id = random.randint(1, 0xFFFF)
        self.last = None

    def note(self, packet):
        if self.last is not None and packet < self.last - 8:
            # Backwards by more than a little reordering could explain.
            self.id = random.randint(1, 0xFFFF)
            self.last = None
            return True
        self.last = packet
        return False


class Clipper:
    """Accumulates decoded audio and files it in clip-sized pieces."""

    def __init__(self, device_id, prefix="omi"):
        self.device_id = device_id
        self.prefix = prefix
        self.pcm = []
        self.have = 0
        self.first_ms = None
        self.last_ms = None
        self.run = Run()
        self.clips = 0
        self.frames = 0

    def add(self, packet, pcm):
        if self.run.note(packet):
            # A reboot mid-clip: what is held belongs to the run that ended,
            # so it is filed before anything new is mixed into it.
            self.flush()

        ms = packet * FRAME_MS
        if self.first_ms is None:
            self.first_ms = ms
        self.last_ms = ms

        self.pcm.append(pcm)
        self.have += len(pcm)
        self.frames += 1

        if self.have >= CLIP_SECONDS * RATE:
            self.flush()

    def flush(self):
        if not self.pcm:
            return None
        audio = np.concatenate(self.pcm)
        seconds = len(audio) / float(RATE)
        ended = time.time()

        # Written before the buffer is cleared, so a failure here costs
        # nothing: the audio is still held and the next flush tries again.
        path = clipwriter.save_wav(self.prefix, ended, audio, RATE)
        clipwriter.write_times(
            path, started=ended - seconds, ended=ended, seconds=seconds,
            source="omi", first_ms=self.first_ms, last_ms=self.last_ms,
            boot_id=self.run.id, device_id=self.device_id,
            # Arrival time, because an Omi has no clock to ask and nothing
            # tells it one. Close enough while connected -- the audio is
            # seconds old -- and honestly marked so nothing downstream reads
            # it as the device's own account of when this happened.
            time_known=False)

        self.pcm = []
        self.have = 0
        self.first_ms = self.last_ms = None
        self.clips += 1
        return path


async def find_omi(timeout=15.0):
    devs = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (d, adv) in devs.items():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if OMI_SERVICE in uuids:
            out.append((addr, adv.local_name or d.name or "Omi", adv.rssi))
    return sorted(out, key=lambda t: -t[2])


async def capture(address, seconds=None, quiet=False, on_progress=None):
    """Stream from one Omi until interrupted, filing clips as it goes."""
    device_id = norm_id(address)
    clipper = Clipper(device_id)
    reboots = [0]
    dropped = [0]

    async with BleakClient(address, timeout=25.0) as client:
        frame_samples = 320
        try:
            raw = await client.read_gatt_char(OMI_CODEC)
            frame_samples = CODEC_FRAMES.get(raw[0], 320)
            if not quiet:
                print(f"codec {raw[0]} -> {frame_samples} samples "
                      f"({frame_samples * 1000 // RATE} ms)")
        except Exception:
            pass                     # the default is what a CV 1 uses

        def on_frame(_h, data):
            data = bytes(data)
            if len(data) <= HEADER_LEN:
                return
            packet = data[0] | (data[1] << 8)
            index = data[2]
            if index != 0:
                # A frame split across notifications. Not seen on a CV 1 at
                # this MTU, and reassembling half a frame guessing at the
                # other half would be worse than skipping it.
                dropped[0] += 1
                return
            try:
                pcm = decode_opus(data[HEADER_LEN:], frame_samples, RATE)
            except Exception:
                dropped[0] += 1
                return
            before = clipper.run.id
            clipper.add(packet, pcm)
            if clipper.run.id != before:
                reboots[0] += 1

        await client.start_notify(OMI_AUDIO, on_frame)
        if not quiet:
            print(f"recording from {address} -- ctrl-c to stop")

        started = time.time()
        try:
            while client.is_connected:
                await asyncio.sleep(1.0)
                if seconds and time.time() - started >= seconds:
                    break
                if on_progress:
                    # So whoever started this can say it is still alive
                    # rather than only that it once began.
                    on_progress({"clips": clipper.clips,
                                 "frames": clipper.frames})
                if not quiet and clipper.frames and clipper.frames % 500 == 0:
                    print(f"  {clipper.frames} frames, {clipper.clips} clip(s)")
        finally:
            # Whatever is held belongs in the archive, not in memory. This is
            # the difference between stopping and losing the last half minute.
            try:
                await client.stop_notify(OMI_AUDIO)
            except Exception:
                pass
            last = clipper.flush()
            if not quiet:
                print(f"\n{clipper.frames} frames, {clipper.clips} clip(s), "
                      f"{reboots[0]} reboot(s), {dropped[0]} unusable")
                if last:
                    print(f"last clip: {os.path.basename(last)}")
    return clipper


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="list Omi devices")
    ap.add_argument("--address", help="the device to record from")
    ap.add_argument("--seconds", type=int, help="stop after this long")
    args = ap.parse_args()

    if args.scan or not args.address:
        found = asyncio.run(find_omi())
        if not found:
            print("no Omi device advertising")
            return 1
        for addr, name, rssi in found:
            print(f"{addr}  rssi={rssi:>4}  {name}")
        if args.scan:
            return 0
        args.address = found[0][0]
        print(f"\nusing {args.address}")

    try:
        asyncio.run(capture(args.address, seconds=args.seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
