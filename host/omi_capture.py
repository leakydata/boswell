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

# Everything else the device will tell you about itself. Named from their
# firmware rather than guessed from the numbers -- omi/src/lib/core/
# transport.c declares a settings service, a features service and a time
# service, and without those names this is a list of hex strings.
OMI_DIM_RATIO  = "19b10011-e8f2-537e-4f6c-d104768a1214"   # LED brightness, rw
OMI_MIC_GAIN   = "19b10012-e8f2-537e-4f6c-d104768a1214"   # microphone gain, rw
OMI_CHARGING   = "19b10013-e8f2-537e-4f6c-d104768a1214"   # 1 while charging
OMI_FEATURES   = "19b10021-e8f2-537e-4f6c-d104768a1214"   # capability bits
OMI_TIME_READ  = "19b10032-e8f2-537e-4f6c-d104768a1214"   # its clock, epoch
OMI_TIME_WRITE = "19b10031-e8f2-537e-4f6c-d104768a1214"   # set its clock

BATTERY   = "00002a19-0000-1000-8000-00805f9b34fb"
MODEL     = "00002a24-0000-1000-8000-00805f9b34fb"
FIRMWARE  = "00002a26-0000-1000-8000-00805f9b34fb"
HARDWARE  = "00002a27-0000-1000-8000-00805f9b34fb"
MAKER     = "00002a29-0000-1000-8000-00805f9b34fb"


async def read_stats(client):
    """Everything the device will say about itself, in one pass.

    Read on a connection somebody else already has, because the radio is
    exclusive: there is no second reader, so whatever holds the device is the
    only thing that can ask. Every field is optional -- a firmware that does
    not offer one is not an error, and a stats read must never be the reason
    a recording stops.
    """
    out = {}

    async def get(uuid, name, fn):
        try:
            out[name] = fn(await client.read_gatt_char(uuid))
        except Exception:
            pass

    def text(v):
        return v.decode("utf-8", "replace").strip("\x00").strip()

    def u8(v):
        return v[0] if v else None

    def le32(v):
        return int.from_bytes(v[:4], "little") if len(v) >= 4 else None

    await get(BATTERY,      "battery",   u8)
    await get(OMI_CHARGING, "charging",  lambda v: bool(u8(v)))
    await get(OMI_MIC_GAIN, "mic_gain",  u8)
    await get(OMI_DIM_RATIO, "dim_ratio", u8)
    await get(OMI_FEATURES, "features",  le32)
    await get(OMI_CODEC,    "codec",     u8)
    await get(OMI_TIME_READ, "device_epoch", le32)
    await get(MODEL,        "model",     text)
    await get(FIRMWARE,     "firmware",  text)
    await get(HARDWARE,     "hardware",  text)
    await get(MAKER,        "maker",     text)

    if out.get("codec") in CODEC_FRAMES:
        out["codec_name"] = f"Opus {CODEC_FRAMES[out['codec']] * 1000 // RATE} ms"
    return out


async def read_volatile(client):
    """The facts about an Omi that change while it is running.

    read_stats() is a one-pass read of everything, and most of what it
    returns -- model, firmware, maker -- is the same on the last day of the
    device's life as on the first. Battery is not: it is read once when a
    session opens and then shown unchanged for as long as the link holds,
    which is how the panel came to say 100% for hours while the cell drained.

    So this is the short list worth asking again, and it is deliberately
    short: every read competes with the audio notifications on the same
    radio.
    """
    out = {}

    async def get(uuid, name, fn):
        try:
            out[name] = fn(await client.read_gatt_char(uuid))
        except Exception:
            pass                 # never the reason a recording stops

    await get(BATTERY,       "battery",      lambda v: v[0] if v else None)
    await get(OMI_CHARGING,  "charging",     lambda v: bool(v[0]) if v else None)
    await get(OMI_TIME_READ, "device_epoch",
              lambda v: int.from_bytes(v[:4], "little") if len(v) >= 4 else None)
    if out:
        # When this reading was taken, so a reader comparing the device clock
        # against something measures drift rather than the age of the reading.
        out["read_at"] = time.time()
    return out


async def apply_settings(client, mic_gain=None, dim_ratio=None):
    """Change what can be changed on a running Omi.

    Both of these are read-write in their firmware and neither survives being
    guessed at: gain is what the audio quality depends on, and the LED is what
    tells a room it is being recorded. Written on a connection somebody else
    already has, for the same reason everything else here is -- the radio is
    exclusive, so whatever holds the device is the only thing that can ask it
    anything.

    Returns the fields it managed to write. A refusal is reported rather than
    raised: a setting that would not take is not a reason to stop recording.
    """
    done = {}
    for uuid, name, value in ((OMI_MIC_GAIN,  "mic_gain",  mic_gain),
                              (OMI_DIM_RATIO, "dim_ratio", dim_ratio)):
        if value is None:
            continue
        try:
            v = max(0, min(255, int(value)))
            await client.write_gatt_char(uuid, bytes([v]), response=True)
            # Read back rather than trust the write: a firmware that clamps
            # or ignores a value would otherwise leave the interface showing
            # a number the device never held.
            done[name] = (await client.read_gatt_char(uuid))[0]
        except Exception:
            pass
    return done


async def set_clock(client, epoch=None):
    """Tell it the time.

    Worth doing because the packets it stores are stamped with its own clock,
    and those stamps are what make offloaded audio placeable at all. A device
    whose clock has drifted or reset files real conversations under the wrong
    hour, and nothing downstream can tell.
    """
    import time as _t
    epoch = int(epoch if epoch is not None else _t.time())
    await client.write_gatt_char(OMI_TIME_WRITE,
                                 epoch.to_bytes(4, "little"), response=True)
    return epoch

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


async def capture(address, seconds=None, quiet=False, on_progress=None,
                  should_stop=None, on_stats=None, stats_every=60,
                  on_tick=None):
    """Stream from one Omi until interrupted, filing clips as it goes.

    `on_stats` is handed the readings that change while it runs -- battery
    above all -- every `stats_every` seconds, because the radio is exclusive
    and this loop is the only thing holding the device.

    `on_tick` is awaited once a second with the live client, so a caller can
    write to the device at all. Nothing else can: the connection this loop
    holds is the only one there is.
    """
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
        last_stats = 0.0
        try:
            while client.is_connected:
                await asyncio.sleep(1.0)
                # Asked to stop, by a signal or a service restart. Without
                # this the loop only ends when the device goes away, so a
                # `systemctl restart` waited out its timeout and the process
                # was SIGKILLed -- taking whatever was held in the clipper
                # with it, which is the one thing stopping cleanly is for.
                if should_stop and should_stop():
                    break
                if seconds and time.time() - started >= seconds:
                    break
                if on_progress:
                    # So whoever started this can say it is still alive
                    # rather than only that it once began.
                    on_progress({"clips": clipper.clips,
                                 "frames": clipper.frames})
                if on_tick:
                    try:
                        await on_tick(client)
                    except Exception:
                        pass     # never the reason a recording stops
                if on_stats and time.time() - last_stats >= stats_every:
                    last_stats = time.time()
                    try:
                        fresh = await read_volatile(client)
                    except Exception:
                        fresh = {}   # a stats read never stops a recording
                    if fresh:
                        on_stats(fresh)
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
    # Carried out on the clipper so the caller can report them. A counter
    # reset means the recorder restarted mid-session, which is the one fact
    # that distinguishes "you walked away" from "it is dying", and it was
    # only ever printed to a log.
    clipper.reboots = reboots[0]
    clipper.dropped = dropped[0]
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
