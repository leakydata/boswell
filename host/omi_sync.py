#!/usr/bin/env python3
"""Pull what an Omi recorded while it was away from the computer.

    uv run host/omi_sync.py --info        # what is waiting
    uv run host/omi_sync.py               # bring it in
    uv run host/omi_sync.py --discard     # drop what is held, unread

The device keeps a ring of raw packets on its card and hands them over on
request. This is the counterpart to live capture: live is what it heard while
connected, this is everything else.

The protocol is theirs and is in their firmware -- omi/src/lib/core/storage.c
-- read over the control characteristic as notifications:

    -> 0x10                              ring info
    <- 0x02 read:u64 write:u64 cap:u32 dropped:u64 pktbytes:u16   (big-endian)
    -> 0x11 start:u64 [count:u32]        read
    <- 0x05                              read begins
    <- 0x03 <payload>                    ... repeated
    <- 0x04 status:u8 next:u64           done
    -> 0x12 seq:u64                      mark read up to here
    <- 0x01 status:u8                    acknowledged

Each packet is 444 bytes: a big-endian Unix timestamp, then Opus frames
packed as [len:u8][frame]. Which means offloaded audio knows when it
happened, where the live stream does not -- the device stamps what it stores
and says nothing about what it streams.

**Reading consumes.** The device advances its own read pointer as it confirms
bytes sent -- STORAGE_ADVANCE_CHECKPOINT_MS in their firmware -- so a packet
handed over is a packet gone, whatever the host does with it afterwards.
Asking for it again returns "sequence out of range". There is no second
chance and no --keep that means anything.

So the bytes are spooled to disk the moment they arrive, before anything
tries to decode them, and the clips are made from the spool afterwards. A
crash then costs the seconds in flight rather than everything not yet
processed. The spool is kept until its audio is in the archive, which also
makes an interrupted run resume rather than restart.
"""

import argparse
import asyncio
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))

import numpy as np
from bleak import BleakClient, BleakScanner

from ble_capture import decode_opus
from omi_capture import OMI_SERVICE, norm_id
import clipwriter
import dedup

CTRL = "30295781-4301-eabd-2904-2849adfeae43"

CMD_INFO, CMD_READ, CMD_ADVANCE = 0x10, 0x11, 0x12
N_ACK, N_INFO, N_DATA, N_DONE, N_BEGIN = 0x01, 0x02, 0x03, 0x04, 0x05

STATUS = {0: "ok", 6: "invalid command", 9: "storage not ready",
          10: "sequence out of range"}

PACKET_BYTES = 444
STAMP_BYTES = 4
RATE = 16000
FRAME_SAMPLES = 320

# Packets per request. Large enough that the round trip is not the cost,
# small enough that an interruption loses little and the read pointer moves
# often -- which is what makes this resumable.
BATCH = 400

CLIP_SECONDS = 30

# Raw packets land here first and are deleted once their audio is filed.
# Anything left behind is a run that did not finish, and is picked up next
# time rather than lost -- which matters more than usual, because the device
# will not hand it over twice.
SPOOL = os.path.join(clipwriter.DATA, "omi_spool")

# One synthetic run for everything offloaded. The packets carry absolute
# time, so unlike the live stream they need no run to place them: device_ms
# is the timestamp itself in milliseconds, which is unique per device for all
# time and is exactly what de-duplication wants.
OFFLINE_RUN = 1


def held_records():
    """Every (device, span) the archive already holds."""
    out = []
    if not os.path.isdir(clipwriter.TIMES):
        return out
    import json
    for name in os.listdir(clipwriter.TIMES):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(clipwriter.TIMES, name)) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def parse_packet(p):
    """(timestamp, [opus frames]) from one 444-byte stored packet."""
    if len(p) < PACKET_BYTES:
        return None, []
    ts = struct.unpack(">I", p[:STAMP_BYTES])[0]
    frames, off = [], STAMP_BYTES
    while off < PACKET_BYTES:
        n = p[off]
        # A zero length is the padding at the end of a partly filled packet,
        # not a frame. So is anything claiming to run past the end.
        if n == 0 or off + 1 + n > PACKET_BYTES:
            break
        frames.append(p[off + 1:off + 1 + n])
        off += 1 + n
    return ts, frames


class Sink:
    """Turns decoded packets into clips, filed with the time they happened."""

    def __init__(self, device_id, held=None):
        self.device_id = device_id
        self.pcm, self.have = [], 0
        self.first_ts = self.last_ts = None
        self.clips = self.frames = self.bad = 0
        self.skipped = 0
        # What the archive already has, so a second sync of the same ring --
        # after --keep, or after an interruption re-read a few seconds -- is
        # not a second copy of the same conversation.
        self.held = held if held is not None else []

    def add(self, ts, frames):
        """Returns True if a clip was completed."""
        before = self.clips
        for f in frames:
            try:
                self.pcm.append(decode_opus(bytes(f), FRAME_SAMPLES, RATE))
            except Exception:
                self.bad += 1
                continue
            self.have += FRAME_SAMPLES
            self.frames += 1
        if frames:
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
        # A gap of more than a minute is a different sitting, not a longer
        # clip: the device stops recording when it hears nothing, and
        # splicing across that would invent a conversation.
        if self.have >= CLIP_SECONDS * RATE:
            self.flush()
        return self.clips != before

    def flush(self):
        if not self.pcm:
            return None
        audio = np.concatenate(self.pcm)
        seconds = len(audio) / float(RATE)
        started = float(self.first_ts)
        ended = started + seconds
        span = (int(self.first_ts) * 1000, int(self.last_ts) * 1000)

        if dedup.is_duplicate(OFFLINE_RUN, span, self.held,
                              device_id=self.device_id):
            self.pcm, self.have = [], 0
            self.first_ts = self.last_ts = None
            self.skipped += 1
            return None

        path = clipwriter.save_wav("omi", ended, audio, RATE)
        rec = clipwriter.write_times(
            path, started=started, ended=ended, seconds=seconds,
            source="omi-card",
            # The stored timestamp in milliseconds. Absolute, so it is unique
            # for this device for all time -- no run id needed to place it.
            first_ms=int(self.first_ts) * 1000,
            last_ms=int(self.last_ts) * 1000,
            boot_id=OFFLINE_RUN, device_id=self.device_id,
            # This is the device's own account of when it heard this, which
            # is the whole difference between offloaded and live audio.
            time_known=True)

        # So the next clip in this same run is matched against it too.
        self.held.append(rec)
        self.pcm, self.have = [], 0
        self.first_ts = self.last_ts = None
        self.clips += 1
        return path


class Link:
    """One connection's worth of the storage protocol."""

    def __init__(self, client):
        self.c = client
        self.info = None
        self.data = bytearray()
        self.done = None
        self.ack = None
        self._begun = asyncio.Event()

    def on_notify(self, _h, raw):
        d = bytes(raw)
        if not d:
            return
        kind = d[0]
        if kind == N_DATA:
            self.data.extend(d[1:])
        elif kind == N_DONE and len(d) >= 10:
            self.done = struct.unpack(">Q", d[2:10])[0]
        elif kind == N_BEGIN:
            self._begun.set()
        elif kind == N_ACK and len(d) >= 2:
            self.ack = d[1]
        elif kind == N_INFO and len(d) >= 31:
            read_seq, write_seq = struct.unpack(">QQ", d[1:17])
            self.info = {
                "read_seq": read_seq, "write_seq": write_seq,
                "capacity": struct.unpack(">I", d[17:21])[0],
                "dropped": struct.unpack(">Q", d[21:29])[0],
                "packet_bytes": struct.unpack(">H", d[29:31])[0],
            }

    async def _wait(self, check, timeout):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            if check():
                return True
            await asyncio.sleep(0.05)
        return False

    async def ring_info(self, timeout=8.0):
        self.info = None
        await self.c.write_gatt_char(CTRL, bytes([CMD_INFO]), response=True)
        if not await self._wait(lambda: self.info is not None, timeout):
            raise RuntimeError("the device did not answer the ring query")
        return self.info

    async def read(self, start, count, timeout=90.0):
        """Returns the raw bytes, and the sequence the device stopped at."""
        self.data, self.done, self.ack = bytearray(), None, None
        cmd = (bytes([CMD_READ]) + struct.pack(">Q", start)
               + struct.pack(">I", count))
        await self.c.write_gatt_char(CTRL, cmd, response=True)
        ok = await self._wait(
            lambda: self.done is not None or self.ack is not None, timeout)
        if self.ack is not None and self.ack != 0:
            raise RuntimeError(f"device refused the read: "
                               f"{STATUS.get(self.ack, self.ack)}")
        if not ok:
            raise RuntimeError("the device stopped sending")
        return bytes(self.data), self.done

    async def advance(self, seq, timeout=8.0):
        """Mark everything below `seq` as taken. Only ever called once the
        audio is on disk -- the device's read pointer is the record of what
        has been collected, and moving it early is how audio is lost."""
        self.ack = None
        await self.c.write_gatt_char(
            CTRL, bytes([CMD_ADVANCE]) + struct.pack(">Q", seq), response=True)
        await self._wait(lambda: self.ack is not None, timeout)
        return self.ack


async def find_omi(timeout=12.0):
    devs = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (d, adv) in devs.items():
        if OMI_SERVICE in [u.lower() for u in (adv.service_uuids or [])]:
            return addr
    return None


async def sync(address, mark_read=True, limit_packets=None, quiet=False,
               progress=None, dev=None, on_ring=None, on_client=None):
    """Pull the ring to a spool file, then turn the spool into clips."""
    device_id = norm_id(address)
    os.makedirs(SPOOL, exist_ok=True)
    took = 0
    t0 = time.time()
    spool_path = os.path.join(SPOOL, f"{device_id}_{int(time.time())}.raw")

    async with BleakClient(dev or address, timeout=30.0) as client:
        link = Link(client)
        await client.start_notify(CTRL, link.on_notify)
        await asyncio.sleep(0.5)

        # Anything else the caller wants from the device, asked here rather
        # than on a connection of its own. The radio is exclusive and this
        # recorder advertises in short windows, so every extra connection is
        # another search that can fail -- and the readings one did were
        # failing on every session while this one succeeded seconds later.
        if on_client:
            try:
                await on_client(client)
            except Exception as e:
                print(f"readings: {type(e).__name__}: {e}", flush=True)

        info = await link.ring_info()
        # Reported from here because here is where it is already known. A
        # separate connection asking the same question kept failing and
        # nobody saw, so the panel showed a figure twenty-one hours old
        # beside a battery reading a minute old, and it read as current.
        if on_ring:
            try:
                on_ring(info)
            except Exception:
                pass
        waiting = info["write_seq"] - info["read_seq"]
        if not quiet:
            mb = waiting * info["packet_bytes"] / 1024 / 1024
            print(f"{waiting} packet(s) waiting, {mb:.1f} MB"
                  + (f", {info['dropped']} dropped" if info["dropped"] else ""))
        if waiting <= 0:
            return None, 0

        target = waiting if limit_packets is None else min(waiting, limit_packets)
        seq = info["read_seq"]
        end = seq + target

        with open(spool_path, "wb") as spool:
            while seq < end:
                count = min(BATCH, end - seq)
                raw, nxt = await link.read(seq, count)
                if not raw:
                    break

                # Down and durable before anything else happens to it. The
                # device has already let go of these packets by the time they
                # arrive; losing them to a decode error or a crash would lose
                # them for good.
                spool.write(raw)
                spool.flush()
                os.fsync(spool.fileno())

                got = len(raw) // PACKET_BYTES
                seq = nxt if nxt else seq + got
                took += got

                if mark_read:
                    # Belt and braces. The device advances on its own as it
                    # confirms bytes; this makes the last partial batch
                    # explicit rather than relying on that.
                    await link.advance(seq)
                if progress:
                    progress(took, target)
                if not quiet:
                    el = time.time() - t0
                    rate = took * PACKET_BYTES / max(el, 0.01) / 1024
                    print(f"  {took}/{target} packets  {rate:.0f} kB/s")

    return spool_path, took


def drain_spool(device_id, quiet=False):
    """Turn every spooled file into clips, then delete it.

    Separate from the transfer on purpose: the radio work is what cannot be
    repeated, so it is not made to wait on decoding, and a decode that fails
    leaves the bytes where they are for another attempt.
    """
    if not os.path.isdir(SPOOL):
        return None

    sink = Sink(device_id, held=held_records())
    for name in sorted(os.listdir(SPOOL)):
        if not name.endswith(".raw"):
            continue
        path = os.path.join(SPOOL, name)
        with open(path, "rb") as f:
            blob = f.read()
        for i in range(len(blob) // PACKET_BYTES):
            ts, frames = parse_packet(blob[i * PACKET_BYTES:
                                           (i + 1) * PACKET_BYTES])
            if ts:
                sink.add(ts, frames)
        sink.flush()
        os.remove(path)
        if not quiet:
            print(f"  {name}: {sink.clips} clip(s) so far")
    sink.flush()
    return sink


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", help="the Omi to sync")
    ap.add_argument("--info", action="store_true", help="only say what is waiting")
    ap.add_argument("--spool-only", action="store_true",
                    help="fetch the bytes but leave the decoding for later")
    ap.add_argument("--packets", type=int, help="stop after this many")
    ap.add_argument("--discard", action="store_true",
                    help="throw away what is held without reading it")
    args = ap.parse_args()

    addr = args.address or asyncio.run(find_omi())
    if not addr:
        print("no Omi device advertising")
        return 1
    print(f"device {addr}")

    if args.info:
        async def show():
            async with BleakClient(addr, timeout=30.0) as c:
                link = Link(c)
                await c.start_notify(CTRL, link.on_notify)
                await asyncio.sleep(0.5)
                i = await link.ring_info()
                w = i["write_seq"] - i["read_seq"]
                print(f"  {w} packet(s) waiting "
                      f"({w * i['packet_bytes'] / 1024 / 1024:.1f} MB)")
                print(f"  ring {i['read_seq']} .. {i['write_seq']} of "
                      f"{i['capacity']}, {i['dropped']} dropped")
        asyncio.run(show())
        return 0

    if args.discard:
        # Move the read pointer to the write head without asking for the
        # bytes in between. The device frees them; nothing reaches the
        # archive. This exists because audio recorded against a wrong device
        # clock is worse than no audio -- it lands in the archive at a time
        # it did not happen, and no later correction can find it again.
        async def discard():
            async with BleakClient(addr, timeout=30.0) as c:
                link = Link(c)
                await c.start_notify(CTRL, link.on_notify)
                await asyncio.sleep(0.5)
                i = await link.ring_info()
                held = i["write_seq"] - i["read_seq"]
                if held <= 0:
                    print("  nothing held")
                    return 0
                print(f"  discarding {held} packet(s) "
                      f"({held * i['packet_bytes'] / 1024 / 1024:.1f} MB)")
                ack = await link.advance(i["write_seq"])
                print(f"  device said {STATUS.get(ack, ack)}")
                i = await link.ring_info()
                print(f"  ring now {i['read_seq']} .. {i['write_seq']}")
                return 0
        return asyncio.run(discard())

    _, took = asyncio.run(sync(addr, limit_packets=args.packets))
    print(f"\n{took} packet(s) spooled")
    if args.spool_only:
        print("left in the spool; run again to turn them into clips")
        return 0
    sink = drain_spool(norm_id(addr))
    if sink:
        print(f"{sink.frames} frames, {sink.bad} unusable, {sink.clips} "
              f"clip(s), {sink.skipped} already held")
    return 0


if __name__ == "__main__":
    sys.exit(main())
