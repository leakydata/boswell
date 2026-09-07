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

# What was last learned about the device, kept across sessions.
#
# `stats` used to be rebuilt empty on every session, so the moment a device
# stopped answering the last thing it had said about itself was thrown away.
# "the battery was 96% at 17:00" is exactly what somebody wants when a
# recorder vanishes, and it was being discarded at the point it became
# useful.
LAST = {"stats": {}, "session": None}


def _restore_last():
    """Seed what was last known from the status file this daemon wrote before.

    Kept in memory only, this was lost on every restart -- and a restart is
    exactly when somebody is trying to work out what happened. The state is
    not carried over, because that was true of the previous run and says
    nothing about this one; the readings and the last session are facts about
    the device and survive.
    """
    try:
        with open(STATUS) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return
    if isinstance(d.get("stats"), dict):
        LAST["stats"] = d["stats"]
    last = d.get("last_session")
    # A session with no frames and no clips was never a session. Written by
    # an older build that recorded every failed connection attempt as one,
    # so restoring it would carry that bug's output across the upgrade that
    # fixed it -- and it would sit on screen describing a link that was
    # never made until a real session happened to replace it.
    if isinstance(last, dict) and (last.get("frames") or last.get("clips")):
        LAST["session"] = last


_restore_last()


def publish(**fields):
    fields["at"] = time.time()
    fields.setdefault("stats", LAST["stats"] or None)
    fields["last_session"] = LAST["session"]
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


def clear_wanted(applied_id):
    """Consume the request, so it is carried out once and not again.

    Left in place it becomes a standing wish, reasserted on every
    reconnection. That has an argument for it -- a device that rebooted to
    its defaults gets your settings back -- but it also means a change made
    once quietly outlives the moment it was made, and there is no way to see
    that from the interface, which shows only what the device currently
    holds.

    Only removed if the file still names the request that was applied: a
    newer one may have been written between reading and getting here, and
    deleting that would drop a change nobody carried out.
    """
    still = read_wanted()
    if not still or still.get("id") != applied_id:
        return
    try:
        os.remove(WANTED)
    except OSError:
        pass


async def one_session(address, quiet=False, dev=None):
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
    # Seeded from the last session rather than started empty, so a reading
    # survives the device going away and can be shown with its age.
    stats = dict(LAST["stats"])
    try:
        from bleak import BleakClient
        async with BleakClient(dev or address, timeout=25.0) as c:
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

    # "connecting", not "syncing" -- nothing has been reached yet.
    #
    # This published "syncing" before the sync was attempted, so a device
    # that was not there at all produced a fresh status file saying syncing
    # every retry. The file therefore never went stale, "syncing" is not one
    # of the states the interface treats as away, and the recorder was
    # reported as connected and transferring for hours while every single
    # attempt failed with "device not found". The owner watched it say
    # syncing all evening and reasonably asked why nothing arrived.
    #
    # The honest report while trying is "connecting". "syncing" is published
    # from the progress callback below, which only runs once the client is
    # connected and packets are actually moving.
    publish(state="connecting", address=address, stats=stats)
    try:
        # While the storage characteristic is the thing being talked to
        # anyway, and before any audio is flowing.
        try:
            await read_ring_now(address, stats)
        except Exception:
            pass                       # a panel figure, never a reason to stop
        spool, took = await omi_sync.sync(
            address, quiet=quiet, dev=dev,
            progress=lambda done, total: publish(
                state="syncing", address=address, stats=stats,
                done=done, total=total))
        if took:
            sink = omi_sync.drain_spool(device_id, quiet=True)
            n = sink.clips if sink else 0
            print(f"synced {took} packet(s) -> {n} clip(s)", flush=True)
            # How much audio a stored packet is worth, measured rather than
            # assumed. Packets are a fixed 444 bytes but hold a variable
            # number of 20 ms frames -- a partly filled one is padded -- so
            # the only honest way to turn "packets waiting" into "minutes
            # waiting" is to divide what this device actually sent.
            if sink and sink.frames:
                stats["seconds_per_packet"] = round(
                    sink.frames * 0.02 / took, 4)
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

    linked = asyncio.Event()

    async def heartbeat():
        # Nothing is published until the link exists. The beat used to start
        # with the attempt, so a session that never reached the device still
        # announced a state -- first "recording", then "connecting" once that
        # was fixed, both of them describing an intention rather than a
        # connection. The honest report while trying is the one the retry
        # loop already publishes.
        await linked.wait()
        while not stop.is_set():
            # "recording" only once audio has actually arrived.
            #
            # This published it the moment the beat started, which is before
            # capture has connected, let alone received anything. A session
            # that failed to find the device therefore announced itself as
            # recording, and did so every twenty seconds against a retry
            # cycle of twenty-five -- so the interface spent most of its time
            # claiming to record from a device that was not there, and the
            # owner reasonably asked why no clips were arriving.
            #
            # A thing that stopped must not look like a thing that works.
            publish(state=("recording" if beat["frames"] else "connecting"),
                    address=address, stats=stats,
                    clips=beat["clips"], frames=beat["frames"])
            try:
                await asyncio.wait_for(stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    # Applied by id rather than by comparing values, so asking for the gain
    # the device already has is still an instruction that completes -- the
    # interface is waiting to hear that it took, and "no change needed" looks
    # exactly like "never arrived" to whoever is watching the slider.
    applied = {"id": None}


    async def on_tick(client):
        # The ring used to be read from here, which meant every session
        # opened a second notification subscription and sent a storage
        # command one second into the audio stream -- `ring["at"]` starts at
        # zero, so the "every sixty seconds" test was true immediately.
        #
        # Recording stopped working that afternoon and stayed broken:
        # sessions connected, received no frames and dropped. The storage
        # protocol is for offload and this was using it underneath a live
        # stream, on a device whose firmware never promised that would work.
        #
        # It is read during the sync instead, which is where the storage
        # characteristic is already in use and nothing is streaming. Some
        # freshness is lost. Recording is the product; a figure on a panel
        # is not.
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
        clear_wanted(want["id"])

    beat = {"clips": 0, "frames": 0}
    began = time.time()
    pulse = asyncio.create_task(heartbeat())
    clipper = None
    try:
        clipper = await omi_capture.capture(address, quiet=quiet, dev=dev,
                                            on_progress=beat.update,
                                            should_stop=lambda: stopping,
                                            # Merged into the dict the
                                            # heartbeat already publishes, so
                                            # the next beat carries the new
                                            # battery without another path
                                            # through the status file.
                                            on_stats=stats.update,
                                            on_tick=on_tick,
                                            on_connected=linked.set)
    finally:
        stop.set()
        linked.set()          # so a beat waiting on it can exit
        await pulse
        LAST["stats"] = dict(stats)
        # What this session was, kept where the interface can read it. A link
        # that drops is not self-explanatory: whether it ran for two minutes
        # or two hours, and whether the recorder restarted underneath it, are
        # the difference between "you walked out of range" and "it is running
        # out of battery".
        #
        # Only a session that actually happened. A connection that was never
        # made raises here too, and recording that as a session overwrote the
        # useful record -- half a minute, no clips, no frames -- with a
        # description of a link that never existed. The retry loop attempts
        # one of those every twenty-five seconds, so the real last session
        # survived about that long.
        #
        # `clipper` is returned only when capture finished on its own; frames
        # are counted while it streams. Either means the device was really
        # there, and a session that streamed and then died by exception is
        # exactly the one worth keeping.
        if clipper is not None or beat.get("frames"):
            LAST["session"] = {
                "began": began, "ended": time.time(),
                "seconds": round(time.time() - began, 1),
                "clips": beat.get("clips", 0), "frames": beat.get("frames", 0),
                "reboots": getattr(clipper, "reboots", None),
                "dropped": getattr(clipper, "dropped", None),
                # Whether it ended on its own terms or was cut off, which is
                # the difference between a service restart and a device that
                # walked away.
                "clean": clipper is not None,
            }
    return clipper


async def read_ring_now(address, stats):
    """How much the device is holding, asked while nothing is streaming.

    On its own connection and its own moment. Doing this underneath a live
    audio stream is what broke recording: the storage protocol is meant for
    offload, and using it concurrently is not something their firmware ever
    offered.
    """
    from bleak import BleakClient
    async with BleakClient(address, timeout=20.0) as c:
        link = omi_sync.Link(c)
        await c.start_notify(omi_sync.CTRL, link.on_notify)
        info = await link.ring_info(timeout=6.0)
        held = max(0, info["write_seq"] - info["read_seq"])
        spp = stats.get("seconds_per_packet")
        stats["storage"] = {
            "held_packets": held,
            "held_bytes": held * info["packet_bytes"],
            "held_seconds": round(held * spp, 1) if spp else None,
            "capacity_packets": info["capacity"],
            "capacity_seconds": (round(info["capacity"] * spp, 1)
                                 if spp else None),
            "dropped": info["dropped"],
            "packet_bytes": info["packet_bytes"],
            "at": time.time(),
        }


async def wait_until_advertising(address, timeout):
    """Listen until the recorder announces itself, then return at once.

    The loop used to sleep a fixed backoff between attempts, which meant it
    was deaf for about sixty seconds of every hundred and forty-five. A
    recorder that advertises only briefly -- after a button press, or in a
    window between power-saving sleeps -- can pass entirely inside that gap,
    and did: several presses in a row produced nothing because nothing was
    listening when the device spoke.

    Sleeping is the wrong shape for waiting on something that appears without
    warning. Listening costs the same and catches it.

    Returns the device it saw, or None if the wait ran out.

    What it saw, rather than merely that it saw something: connecting by
    address makes bleak go and discover the device all over again, and this
    recorder advertises in windows too short to find twice. Handing the
    object straight to BleakClient skips that second search.
    """
    # If BlueZ is already holding it, no advertisement is ever coming: a
    # connected peripheral does not advertise, so waiting for one is waiting
    # forever. Ask first, and take the link that already exists.
    held = await omi_capture.bluez_device(address)
    if held is not None:
        return held

    from bleak import BleakScanner
    want = omi_capture.norm_id(address)
    seen = asyncio.Event()
    found = {"dev": None}

    def on_seen(dev, _adv):
        if omi_capture.norm_id(dev.address) == want:
            found["dev"] = dev
            seen.set()

    scanner = BleakScanner(detection_callback=on_seen)
    try:
        await scanner.start()
    except Exception:
        # No scanner, no cleverness: fall back to the old behaviour rather
        # than turning a retry loop into a crash loop.
        await asyncio.sleep(timeout)
        return None
    try:
        await asyncio.wait_for(seen.wait(), timeout)
        return found["dev"]
    except asyncio.TimeoutError:
        return None
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass


async def run(address=None, quiet=False):
    tries = 0
    # What the last wait actually saw, if it saw anything. Used for the one
    # session that follows the sighting and then dropped: a device object is
    # a handle on a device that was there a moment ago, and reusing a stale
    # one across later retries would be worse than looking again.
    sighted = None
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

        if addr and sighted is None:
            # The same deadlock reached by a different door: the very first
            # attempt, before any wait has happened.
            sighted = await omi_capture.bluez_device(addr)

        if addr:
            try:
                await one_session(addr, quiet=quiet, dev=sighted)
                tries = 0            # a session that ran is a success
                publish(state="waiting", address=addr)
            except Exception as e:
                print(f"session: {type(e).__name__}: {e}", flush=True)
                publish(state="lost", address=addr, error=str(e)[:120])
        else:
            publish(state="not found")

        sighted = None               # used, and only good for that session

        if stopping:
            break
        wait = BACKOFF[min(tries, len(BACKOFF) - 1)]
        tries += 1
        if addr:
            # Listen through the wait instead of sleeping through it, and go
            # the moment it appears.
            sighted = await wait_until_advertising(addr, wait)
            if sighted is not None:
                print("saw it advertise -- connecting now", flush=True)
                tries = 0
        else:
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
