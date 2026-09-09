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
import subprocess
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

# Whether to look for the device at all.
#
# A standing switch, not a request: WANTED is consumed the moment it is
# applied, which is right for "set the gain to 40" and wrong for "stop
# looking" -- a switch that turns itself back on after one reading is not a
# switch. So this file is read and left alone, and the search is off for
# exactly as long as it says so.
#
# Absent or unreadable means search, so deleting the file and a fresh install
# both behave the way somebody would expect, and a corrupt one fails towards
# recording rather than towards silence.
CONTROL = os.path.join(clipwriter.DATA, "omi_control.json")

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

# Consecutive failed syncs. Zeroed by any sync that completes, including one
# that finds nothing waiting -- reaching the ring and being told it is empty
# is a working sync.
# `at` is the most recent failure; `first` is when the run of them
# started. Publishing one figure as both read as "failing since"
# while actually being "last failed at", so a run of failures an hour
# old announced itself as having started seconds ago.
SYNC_FAIL = {"n": 0, "last": None, "at": None, "first": None}

# Resetting the host Bluetooth stack when sync alone is failing.
#
# 2026-09-09: 37 minutes recorded out of range buffered correctly to the ring,
# and then every read of it failed for eighty minutes -- TimeoutError, "did
# not answer the ring query", org.bluez.Error.InProgress, "failed to discover
# services" -- while live capture kept writing clips the whole time. The
# device was fine, the link was fine at -61 dBm, and the audio was fine. The
# host's own connection state was stale: bluetoothd was logging "No matching
# connection for device". A `systemctl restart bluetooth` fixed it in fifteen
# seconds and the ring answered in 0.1 s. Nothing in this daemon noticed, so
# it retried the identical failing call every ninety seconds until a person
# looked.
#
# The gate matters more than the cure. Sync failing on its own is not enough
# -- when the recorder is genuinely away, sync fails all day and resetting the
# adapter every half hour would drop the owner's keyboard for nothing. The
# signature of a wedged stack is specifically **live audio still arriving
# while sync alone fails**: if the device were out of range, both would fail.
# So a reset needs recent frames as well as repeated sync failures.
HEAL_AFTER = 4                    # consecutive sync failures
HEAL_EVERY = 30 * 60              # never more often than this
HEAL_FRAMES_WITHIN = 20 * 60      # a session must have delivered audio since

HEAL = {"at": None, "n": 0, "last": None}


def _streaming_recently():
    """Did a live session actually receive audio in the last few minutes?

    This is the half of the signature that says the device is present and
    the radio works, which is what makes a host-side reset the right answer
    rather than a destructive guess.
    """
    last = LAST.get("session") or {}
    if not last.get("frames"):
        return False
    ended = last.get("ended") or last.get("began")
    return bool(ended) and (time.time() - ended) < HEAL_FRAMES_WITHIN


def _should_heal():
    if SYNC_FAIL["n"] < HEAL_AFTER:
        return False
    if HEAL["at"] and (time.time() - HEAL["at"]) < HEAL_EVERY:
        return False
    return _streaming_recently()


def _heal_bluetooth():
    """Restart the host Bluetooth stack, then bounce the adapter.

    Deliberately blunt, and deliberately rare. It costs every other BLE
    device on this machine a few seconds -- a keyboard blinks out and comes
    back -- which is worth paying to keep hours of recorded audio
    collectable, and not worth paying on a schedule.

    Failure here is reported and otherwise ignored: a daemon that cannot
    reset the adapter should carry on recording, not exit.
    """
    HEAL["at"] = time.time()
    HEAL["n"] += 1
    print(f"sync has failed {SYNC_FAIL['n']} times while audio kept "
          f"arriving -- resetting the Bluetooth stack", flush=True)
    for cmd in (["sudo", "-n", "systemctl", "restart", "bluetooth"],
                ["sudo", "-n", "hciconfig", "hci0", "down"],
                ["sudo", "-n", "hciconfig", "hci0", "up"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                HEAL["last"] = (f"{' '.join(cmd[2:])}: "
                                f"{(r.stderr or '').strip()[:120]}")
                print(f"  {cmd[2:]} -> {HEAL['last']}", flush=True)
                # A failed restart is worth reporting; a failed adapter
                # bounce after a good restart is not worth aborting for.
                if "restart" in cmd:
                    return False
        except Exception as e:
            HEAL["last"] = f"{type(e).__name__}: {e}"
            print(f"  reset failed: {HEAL['last']}", flush=True)
            return False
        time.sleep(2)
    HEAL["last"] = None
    print("  Bluetooth stack reset; the next sync will retry", flush=True)
    return True


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


# How old a ring reading may be before it is reported as unknown rather than
# as a number.
#
# The storage figure is only written by a sync that reached the device, so a
# run of failed syncs leaves the last good reading sitting there looking
# current. That is this project's oldest failure wearing new clothes -- a
# twenty-one-hour-old storage line beside a battery reading a minute old,
# with nothing to tell them apart. Observed again: a panel reporting 80.6
# seconds held, read at 09:14, while the device had been out of range from
# 09:50 to 10:25 recording to that same ring and every sync since had failed.
RING_STALE_AFTER = 20 * 60


def publish(**fields):
    fields["at"] = time.time()
    fields.setdefault("stats", LAST["stats"] or None)
    fields["last_session"] = LAST["session"]
    # Never a reason a run fails, so it is all best effort.
    try:
        st = fields.get("stats") or {}
        ring = st.get("storage")
        if isinstance(ring, dict) and ring.get("at"):
            age = time.time() - ring["at"]
            ring["age_seconds"] = round(age)
            # Said out loud rather than left to the reader to work out from a
            # timestamp they have to compare against now.
            ring["stale"] = age > RING_STALE_AFTER
        if SYNC_FAIL["n"]:
            fields["sync_failing"] = {
                "consecutive": SYNC_FAIL["n"],
                "last_error": SYNC_FAIL["last"],
                "since": SYNC_FAIL["first"] or SYNC_FAIL["at"],
                "last_at": SYNC_FAIL["at"],
                # So the interface can say "it is being dealt with" rather
                # than only "it is broken".
                "healed": HEAL["n"],
                "healed_at": HEAL["at"],
                "heal_error": HEAL["last"],
            }
    except Exception:
        pass
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


# Re-read at most once a second. The capture loop asks on every tick so it
# can put down a live session promptly, and that is a stat and a small read
# each time -- cheap, but there is no reason to do it at the rate a tight
# loop can ask.
_SEARCH = {"at": 0.0, "on": True}


def searching():
    """False only while something has explicitly turned the search off."""
    now = time.time()
    if now - _SEARCH["at"] < 1.0:
        return _SEARCH["on"]
    try:
        with open(CONTROL) as f:
            d = json.load(f)
        on = bool(d.get("search", True)) if isinstance(d, dict) else True
    except (OSError, ValueError):
        on = True
    _SEARCH.update(at=now, on=on)
    return on


# Set when the device reports its own long press, which is the one gesture
# their firmware acts on: turnoff_all(), mic and transport down. A recorder
# that was switched off deliberately is not a recorder that failed, and the
# difference is the whole point -- the daemon otherwise hunts a device that
# is not coming back and the interface says "lost the link", which reads as
# a fault somebody should chase.
switched_off = {"at": None}

MOMENTS = os.path.join(clipwriter.DATA, "moments.jsonl")


def note_moment(kind, device_id=None, clip=None):
    """A press, kept with the time it happened.

    Every event is written, not only the interesting ones. "Did I turn it
    off, or did it drop?" was unanswerable for two days, and the device knew
    the whole time; a line per press costs nothing and settles it.
    """
    rec = {"at": time.time(), "kind": kind,
           "device_id": device_id, "clip": clip}
    try:
        os.makedirs(clipwriter.DATA, exist_ok=True)
        with open(MOMENTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"moment: {type(e).__name__}: {e}", flush=True)
    return rec


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

    async def read_on(c):
        """The panel figures, taken on the connection the sync is holding.

        These had a connection of their own, opened before the sync and the
        stream. It failed on every session -- "stats: TimeoutError" -- while
        the sync connected successfully seconds later, because a recorder
        that advertises in short windows cannot be found three times over,
        and the first search spent the window. Battery is refreshed again
        during capture anyway; the part that only happens here is the clock.
        """
        stats.update(await omi_capture.read_stats(c))
        # When this reading was taken. The stats are read once a session and
        # then republished unchanged, so anything comparing the device clock
        # against "now" measures how long the session has been running, not
        # how far the clock has drifted -- the panel read "off by 23 min"
        # twenty-three minutes after a clock that was correct when read.
        stats["read_at"] = time.time()
        # Its own clock is what stamps the packets it stores, and those
        # stamps are the only thing that makes offloaded audio placeable. A
        # device whose clock has drifted files real conversations under the
        # wrong hour and nothing downstream can tell.
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
        spool, took = await omi_sync.sync(
            address, quiet=quiet, dev=dev,
            # Asked on the connection the sync is already holding, rather
            # than on one of its own that has to find the device again.
            on_ring=lambda info: note_ring(stats, info),
            on_client=read_on,
            progress=lambda done, total: publish(
                state="syncing", address=address, stats=stats,
                done=done, total=total))
        SYNC_FAIL["n"] = 0
        SYNC_FAIL["last"] = None
        SYNC_FAIL["first"] = None
        if took:
            sink = omi_sync.drain_spool(device_id, quiet=True)
            n = sink.clips if sink else 0
            undated = getattr(sink, "undated", 0) if sink else 0
            print(f"synced {took} packet(s) -> {n} clip(s)"
                  + (f" ({undated} with no timestamp, placed by arrival)"
                     if undated else ""),
                  flush=True)
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
        # Kept where the interface can read it. A sync that fails is not a
        # reason to skip the live stream -- but a sync that has failed every
        # attempt for an hour is the difference between "the backlog will
        # still be there next time" and "the backlog can no longer be
        # collected", and only one of those is worth telling somebody about.
        # Observed: 35 minutes of audio recorded out of range, then every
        # sync since failing on connect or on the ring query, while the panel
        # showed a held-seconds figure read before any of it happened.
        SYNC_FAIL["n"] += 1
        SYNC_FAIL["last"] = f"{type(e).__name__}: {e}".strip().rstrip(":")
        SYNC_FAIL["at"] = time.time()
        if SYNC_FAIL["first"] is None:
            SYNC_FAIL["first"] = SYNC_FAIL["at"]
        # Counting the failures was the whole of the response until now: the
        # figure went into the status file and the same call was retried
        # forever. When live audio is still arriving, the fault is this
        # machine's and it is fixable from here.
        if _should_heal():
            try:
                await asyncio.to_thread(_heal_bluetooth)
            except Exception as e:
                print(f"heal: {type(e).__name__}: {e}", flush=True)
        # That handle is spent. A sighting names a D-Bus object BlueZ made
        # when it saw the device, and BlueZ drops the object once the device
        # goes -- so handing it to the stream after it has already failed
        # turns an honest "not found" into "device 'dev_C4_B3_FD_7F_1E_91'
        # not found" for a connection that has tried nothing. Looking again
        # costs a discovery; reusing a dead path cannot succeed.
        dev = None

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

    def on_button(code):
        """What the device says about its own button.

        Every press is written down. Only the long press changes what the
        daemon does, because it is the only one the firmware itself acts on:
        turnoff_all(), mic and transport down. Saying "switched off at 21:14"
        instead of hunting a device that is not coming back is the whole
        reason this is subscribed to.
        """
        name = omi_capture.BUTTON_EVENTS.get(code, f"button {code}")
        # Press and release bracket every tap, so filing them too would make
        # three lines out of one gesture and bury the gesture.
        if code in (omi_capture.BTN_PRESS, omi_capture.BTN_RELEASE):
            return
        # No clip name: capture() returns the clipper when the session ends,
        # so during the session there is nothing here to ask, and the clip
        # being written has no name until it is flushed anyway. The time is
        # the anchor -- every clip carries the span it covers, so the clip a
        # moment fell inside is a lookup rather than something to store.
        note_moment(name, device_id=device_id)
        print(f"button: {name}", flush=True)
        if code == omi_capture.TAP_LONG:
            switched_off["at"] = time.time()
            publish(state="switched off", address=address, stats=stats)

    try:
        clipper = await omi_capture.capture(address, quiet=quiet, dev=dev,
                                            on_button=on_button,
                                            on_progress=beat.update,
                                            # Off means off now, not at the
                                            # end of whatever this is: the
                                            # reason to stop searching is
                                            # usually that somebody wants the
                                            # radio, and "after this session"
                                            # could be hours.
                                            should_stop=lambda: (stopping
                                                                 or not searching()),
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


def note_ring(stats, info):
    """Record what the device says it is holding.

    Taken from the sync's own connection, where the answer is already in
    hand. This used to open a connection of its own to ask the same
    question, and that connection had to find the device a second time --
    which, for a recorder that advertises in short windows, mostly failed.
    The failure was swallowed as "a panel figure, never a reason to stop",
    so nothing said so: the storage line sat twenty-one hours stale next to
    a battery reading a minute old, with nothing to tell them apart. It was
    read as current, and it said the device was holding nine seconds of
    audio when the device was in fact recording normally.
    """
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
    # Asked before anything is opened. The switch is mostly thrown because
    # somebody wants the radio, and starting a scan to find out whether we
    # were allowed to scan would be the one thing it asked us not to do.
    if not searching():
        return None

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
        # than turning a retry loop into a crash loop. Still in slices, so a
        # host with no working scanner is not also a host where the switch
        # appears to be ignored.
        deadline = time.time() + timeout
        while time.time() < deadline and searching():
            await asyncio.sleep(min(0.5, deadline - time.time()))
        return None
    try:
        # Waited in slices rather than in one go, so the switch can be thrown
        # while we are listening. One long wait_for meant "stop looking" left
        # a scanner running for up to a minute afterwards -- which is the
        # whole of what it was asked to stop, and long enough that somebody
        # would reasonably conclude the button did nothing.
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return None
            if not searching():
                return None
            try:
                await asyncio.wait_for(seen.wait(), min(0.5, left))
                return found["dev"]
            except asyncio.TimeoutError:
                continue
    finally:
        try:
            await scanner.stop()
        except Exception:
            pass


async def run(address=None, quiet=False):
    tries = 0
    # Said once when the switch goes off, rather than on every pass: the loop
    # comes round every couple of seconds and a paused daemon should be quiet
    # in the journal, not the loudest thing in it.
    announced_paused = False
    # What the last wait actually saw, if it saw anything. Used for the one
    # session that follows the sighting and then dropped: a device object is
    # a handle on a device that was there a moment ago, and reusing a stale
    # one across later retries would be worse than looking again.
    sighted = None
    while not stopping:
        if not searching():
            # Nothing scanned, nothing connected, no backoff advanced. The
            # radio is left entirely alone so something else can have it,
            # which is the whole point of the switch.
            if not announced_paused:
                print("search paused; not looking for the Omi", flush=True)
                announced_paused = True
            publish(state="paused", address=address, searching=False)
            tries = 0
            sighted = None
            await asyncio.sleep(2)
            continue
        if announced_paused:
            print("search resumed", flush=True)
            announced_paused = False

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
                publish(state=("switched off" if switched_off["at"]
                               else "waiting"), address=addr)
            except Exception as e:
                print(f"session: {type(e).__name__}: {e}", flush=True)
                # A device that told us it was powering down did not fail.
                # Reporting "lost the link" for a deliberate long press is
                # how somebody comes to chase a fault that is a switch.
                if switched_off["at"]:
                    publish(state="switched off", address=addr)
                else:
                    publish(state="lost", address=addr, error=str(e)[:120])
        else:
            publish(state="not found")

        sighted = None               # used, and only good for that session

        if stopping:
            print("stopping; finishing the clip in hand", flush=True)
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
                # Advertising again means somebody switched it back on. The
                # flag has to clear here rather than on the next successful
                # session, or one long press would mark the recorder off for
                # the rest of the daemon's life.
                if switched_off["at"]:
                    print("back on after a long press", flush=True)
                    switched_off["at"] = None
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
        # Nothing is printed from here. A signal can land in the middle of a
        # print, and writing to the same buffered stream from inside the
        # handler raises "reentrant call inside BufferedWriter" -- which
        # killed the daemon outright, mid-session, exit code 1. The line
        # announcing a clean stop was the thing making it unclean. The loop
        # says it instead, where there is no handler to re-enter.
        #
        # The capture loop checks this every second, so the flush that saves
        # the half-finished clip actually happens. It used to be checked only
        # between sessions, which meant never while recording.

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        asyncio.run(run(args.address, quiet=args.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
