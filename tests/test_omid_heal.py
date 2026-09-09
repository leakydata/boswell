"""Resetting the host Bluetooth stack when sync alone is failing.

The fault this guards against, seen 2026-09-09: 37 minutes recorded out of
range buffered correctly to the ring, then every read of it failed for eighty
minutes while live capture kept writing clips throughout. The device, the
link and the audio were all fine; the host's own connection state was stale.

The cure is blunt -- it restarts a system service and every other BLE device
on the machine feels it -- so these tests are mostly about the gate, not the
cure. A reset must fire for that fault and must **not** fire for the ordinary
case of the recorder simply being somewhere else, which also fails sync
forever and would otherwise drop the owner's keyboard twice an hour all day.
"""
import os
import sys
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omid


def setup_function(_):
    omid.SYNC_FAIL.update({"n": 0, "last": None, "at": None})
    omid.HEAL.update({"at": None, "n": 0, "last": None})
    omid.LAST["session"] = None


def streaming(seconds_ago=60, frames=5000):
    omid.LAST["session"] = {"began": time.time() - seconds_ago - 100,
                            "ended": time.time() - seconds_ago,
                            "frames": frames, "clips": 3}


def test_the_fault_it_was_written_for():
    """Sync failing repeatedly while audio still arrives -> reset."""
    streaming()
    omid.SYNC_FAIL["n"] = omid.HEAL_AFTER
    assert omid._should_heal()


def test_recorder_simply_away_is_not_healed():
    """The common case: nothing streaming, so both halves fail. Leave it.

    This is the one that matters. Out of range for a working day fails every
    sync too, and resetting the adapter on that schedule costs the owner
    their keyboard for nothing and fixes nothing.
    """
    omid.SYNC_FAIL["n"] = 50
    omid.LAST["session"] = None
    assert not omid._should_heal()


def test_stale_session_does_not_count_as_streaming():
    """Audio that arrived hours ago says nothing about the radio now."""
    streaming(seconds_ago=omid.HEAL_FRAMES_WITHIN + 60)
    omid.SYNC_FAIL["n"] = 20
    assert not omid._should_heal()


def test_a_session_that_found_nothing_is_not_streaming():
    """Connecting and receiving no frames is not evidence the device is
    reachable -- it is the other half of the same failure."""
    streaming(frames=0)
    omid.SYNC_FAIL["n"] = 20
    assert not omid._should_heal()


def test_one_or_two_failures_are_ordinary():
    """A missed advertisement window is not a wedged stack."""
    streaming()
    for n in range(omid.HEAL_AFTER):
        omid.SYNC_FAIL["n"] = n
        assert not omid._should_heal(), f"healed after only {n} failures"


def test_not_twice_in_a_row():
    """Rate limited, because the cure is disruptive and the disease is not
    always curable -- a reset that did not help must not become a loop."""
    streaming()
    omid.SYNC_FAIL["n"] = 20
    omid.HEAL["at"] = time.time()
    assert not omid._should_heal()
    omid.HEAL["at"] = time.time() - omid.HEAL_EVERY - 1
    assert omid._should_heal()


def test_a_working_sync_clears_the_count():
    """Zeroed by any sync that reaches the ring, including an empty one."""
    streaming()
    omid.SYNC_FAIL["n"] = 20
    assert omid._should_heal()
    omid.SYNC_FAIL["n"] = 0          # what the success path does
    assert not omid._should_heal()


def test_status_carries_what_was_done_about_it():
    """The interface needs to distinguish "broken" from "being dealt with"."""
    omid.SYNC_FAIL.update({"n": 5, "last": "TimeoutError",
                           "at": time.time()})
    omid.HEAL.update({"n": 2, "at": time.time(), "last": None})
    fields = {}

    # publish() writes a file; the shape of what it would write is the
    # contract the banner reads, so it is asserted directly.
    import json
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    old = omid.STATUS
    try:
        omid.STATUS = tmp
        omid.publish(state="recording", address="AA:BB", stats={})
        with open(tmp) as f:
            fields = json.load(f)
    finally:
        omid.STATUS = old
        os.unlink(tmp)

    sf = fields.get("sync_failing") or {}
    assert sf.get("consecutive") == 5
    assert sf.get("healed") == 2
    assert sf.get("healed_at")
