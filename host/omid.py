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

# Where the daemon says what it is doing, for anything that wants to show it.
# A file rather than a socket because the reader is a web server in another
# process that must not block on us, and because it survives both of us.
STATUS = os.path.join(clipwriter.DATA, "omi_status.json")

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

    publish(state="syncing", address=address)
    try:
        spool, took = await omi_sync.sync(
            address, quiet=quiet,
            progress=lambda done, total: publish(
                state="syncing", address=address, done=done, total=total))
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
            publish(state="recording", address=address,
                    clips=beat["clips"], frames=beat["frames"])
            try:
                await asyncio.wait_for(stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    beat = {"clips": 0, "frames": 0}
    pulse = asyncio.create_task(heartbeat())
    try:
        clipper = await omi_capture.capture(address, quiet=quiet,
                                            on_progress=beat.update)
    finally:
        stop.set()
        await pulse
    return clipper


async def run(address=None, quiet=False):
    tries = 0
    while not stopping:
        addr = address
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
        print("stopping after this session", flush=True)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        asyncio.run(run(args.address, quiet=args.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
