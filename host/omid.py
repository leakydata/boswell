#!/usr/bin/env python3
"""Keep an Omi recording into the archive, without being asked.

    uv run host/omid.py                       # find one and stay with it
    uv run host/omid.py --address C4:...      # a particular one

What the phone app did, minus the phone and the cloud. On every connection it
catches up on whatever the device stored while out of range, then streams
live until the link drops, then waits and does it again.

The order matters. Syncing first means the backlog is coming in while the
device is on the desk and the link is good, and the ring stops growing before
live capture has to compete with it. Streaming first would leave hours of
stored audio behind every time somebody walked back into range.

Written as a loop that expects to fail. A wearable walks out of range, its
battery goes flat, Bluetooth on the host gets restarted -- none of those are
errors, they are Tuesday, and the only correct response to all of them is to
wait a moment and try again.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))

import atomicio
import clipwriter
import omi_capture
import omi_sync
import recorders

# Where the daemon says what it is doing, for anything that wants to show it.
# A file rather than a socket because the reader is a web server in another
# process that must not block on us, and because it survives both of us.
STATUS = os.path.join(clipwriter.DATA, "omi_status.json")

# What somebody has asked the device to become. The interface cannot write to
# the Omi itself -- the radio is exclusive and this process is holding it --
# so a request is left here and applied on the connection that exists. The
# same shape as the status file going the other way, and for the same reason:
# two processes, no shared memory, and a file that survives both of them.
WANTED = os.path.join(clipwriter.DATA, "omi_wanted.json")

# Backing off, in seconds. Quick at first because the usual failure is the
# device being briefly busy; longer after that because the usual failure
# after several tries is that it is not in the room.
BACKOFF = [2, 5, 10, 20, 30, 60]

stopping = False


def publish(**fields):
    fields["at"] = time.time()
    try:
        atomicio.write_json(STATUS, fields)
    except Exception:
        pass          # status is a courtesy; never the reason a run fails


def read_wanted():
    """The settings request, or None. A malformed file is not a request."""
    try:
        with open(WANTED) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get("id") is not None else None


async def one_session(address, quiet=False):
    """Catch up, then stream, until the link goes."""
    device_id = omi_capture.norm_id(address)

    # Anything left in the spool from a run that did not finish. Done before
    # the radio work, because it needs no device and the device may not stay.
    try:
        sink = omi_sync.drain_spool(device_id, quiet=True)
        if sink and sink.clips:
            publish(state="catching up", detail=f"{sink.clips} clip(s) from a "
                                                f"previous run")
    except Exception as e:
        print(f"spool: {type(e).__name__}: {e}", flush=True)

    # Read once per session, on the connection the sync is about to use.
    # There is no second reader -- the radio is exclusive -- so whatever
    # holds the device is the only thing that can ask it anything.
    stats = {}
    try:
        from bleak import BleakClient
        async with BleakClient(address, timeout=25.0) as c:
            stats = await omi_capture.read_stats(c)
            # When this reading was taken. The stats are read once a session
            # and then republished unchanged, so anything comparing the
            # device's clock against "now" measures how long the session has
            # been running, not how far the clock has drifted -- the panel
            # read "off by 23 min" twenty-three minutes after a clock that
            # was correct when it was read.
            stats["read_at"] = time.time()
            # Its own clock is what stamps the packets it stores, and those
            # stamps are the only thing that makes offloaded audio placeable.
            # A device whose clock has drifted files real conversations under
            # the wrong hour and nothing downstream can tell.
            drift = abs((stats.get("device_epoch") or 0) - time.time())
            if stats.get("device_epoch") is None or drift > 120:
                stats["clock_set_to"] = await omi_capture.set_clock(c)
                # What the clock now says, not what it said before the
                # correction: leaving the stale reading in place reported a
                # drift that had just been fixed.
                stats["device_epoch"] = stats["clock_set_to"]
                stats["read_at"] = stats["clock_set_to"]
                print(f"set the device clock (was off by {drift:.0f}s)",
                      flush=True)
    except Exception as e:
        print(f"stats: {type(e).__name__}: {e}", flush=True)

    publish(state="syncing", address=address, stats=stats)
    try:
        spool, took = await omi_sync.sync(
            address, quiet=quiet,
            progress=lambda done, total: publish(
                state="syncing", address=address, stats=stats,
                done=done, total=total))
        if took:
            sink = omi_sync.drain_spool(device_id, quiet=True)
            n = sink.clips if sink else 0
            print(f"synced {took} packet(s) -> {n} clip(s)", flush=True)
    except Exception as e:
        # A sync that fails is not a reason to skip the live stream. The
        # backlog will still be there next time; the conversation happening
        # now will not.
        print(f"sync: {type(e).__name__}: {e}", flush=True)

    # A heartbeat for as long as it records, not one line when it starts.
    #
    # publish() was called once here and never again, so after two minutes of
    # perfectly healthy recording the status file was stale and the interface
    # said "not running" -- which is exactly the failure this project keeps
    # finding, a thing that works looking identical to a thing that stopped.
    # The reader cannot tell a silent daemon from a dead one, so the daemon
    # has to keep speaking.
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            publish(state="recording", address=address, stats=stats,
                    clips=beat["clips"], frames=beat["frames"])
            try:
                await asyncio.wait_for(stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    # Applied by id rather than by comparing values, so asking for the gain
    # the device already has is still an instruction that completes -- the
    # interface is waiting to hear that it took, and "no change needed" looks
    # exactly like "never arrived" to whoever is watching the slider.
    #
    # Per session, deliberately. The request file is a standing wish rather
    # than a one-shot, so the settings are asserted again on every
    # reconnection -- which is what you want on a device that may have
    # rebooted back to its defaults in between, and is how the other recorder
    # already treats its own remembered preferences.
    applied = {"id": None}

    async def on_tick(client):
        want = read_wanted()
        if not want or want["id"] == applied["id"]:
            return
        got = await omi_capture.apply_settings(
            client, mic_gain=want.get("mic_gain"),
            dim_ratio=want.get("dim_ratio"))
        applied["id"] = want["id"]
        # Read back into the published stats, so the interface shows what the
        # device holds rather than what it was asked for.
        stats.update(got)
        stats["applied_id"] = want["id"]
        print(f"applied {got or 'nothing'} (request {want['id']})", flush=True)

    beat = {"clips": 0, "frames": 0}
    pulse = asyncio.create_task(heartbeat())
    try:
        clipper = await omi_capture.capture(address, quiet=quiet,
                                            on_progress=beat.update,
                                            should_stop=lambda: stopping,
                                            # Merged into the dict the
                                            # heartbeat already publishes, so
                                            # the next beat carries the new
                                            # battery without another path
                                            # through the status file.
                                            on_stats=stats.update,
                                            on_tick=on_tick)
    finally:
        stop.set()
        await pulse
    return clipper


async def run(address=None, quiet=False):
    tries = 0
    while not stopping:
        addr = address
        if not addr:
            # A paired recorder is the one to talk to. Taking whichever Omi
            # won the scan is fine with one in the house and wrong the moment
            # there are two -- a neighbour's, or a second of your own -- and
            # recording from the wrong device is not a mistake this would
            # report; it would just quietly file somebody else's day.
            want = recorders.first_of("omi")
            if want and want.get("address"):
                addr = want["address"]
            elif recorders.of_kind("omi"):
                # Paired, but by id only: still ours to look for by address.
                addr = None
            else:
                # Nothing paired. Look, so that a first run has something to
                # offer, but do not connect to it uninvited -- pairing is
                # the person saying which device is theirs.
                publish(state="looking")
                found = await omi_capture.find_omi()
                publish(state="not paired",
                        detail=(f"found {found[0][0]}" if found else None))
                await asyncio.sleep(10)
                continue
        if not addr:
            publish(state="looking")
            found = await omi_capture.find_omi()
            if found:
                addr = found[0][0]
                print(f"found {addr}", flush=True)

        if addr:
            try:
                await one_session(addr, quiet=quiet)
                tries = 0            # a session that ran is a success
                publish(state="waiting", address=addr)
            except Exception as e:
                print(f"session: {type(e).__name__}: {e}", flush=True)
                publish(state="lost", address=addr, error=str(e)[:120])
        else:
            publish(state="not found")

        if stopping:
            break
        wait = BACKOFF[min(tries, len(BACKOFF) - 1)]
        tries += 1
        await asyncio.sleep(wait)

    publish(state="stopped")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", help="the Omi to stay with")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    def stop(*_):
        global stopping
        stopping = True
        # The capture loop checks this every second, so the flush that saves
        # the half-finished clip actually happens. It used to be checked only
        # between sessions, which meant never while recording.
        print("stopping; finishing the clip in hand", flush=True)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        asyncio.run(run(args.address, quiet=args.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
