"""Three ways the daemon told the truth badly.

Each of these was believed by somebody acting on it, which is the test that
matters: a number with no age is a claim about now, and a count that does not
say what it is counting invites the wrong response.
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omid
import omi_sync


def _publish(**kw):
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    old = omid.STATUS
    try:
        omid.STATUS = tmp
        omid.publish(**kw)
        with open(tmp) as f:
            return json.load(f)
    finally:
        omid.STATUS = old
        os.unlink(tmp)


# --- the battery ---------------------------------------------------------
# 2026-09-13: published "1%" from a reading 61 minutes old while the device
# sat on a charger at 93%. It was believed and became an urgent instruction.

def test_a_battery_reading_carries_its_age():
    st = {"battery": 1, "read_at": time.time() - 3660}
    out = _publish(state="recording", address="AA:BB", stats=st)
    s = out["stats"]
    assert s["battery_age_seconds"] > 3600
    assert s["battery_stale"] is True, "an hour-old reading must say so"


def test_a_fresh_battery_reading_is_not_flagged():
    st = {"battery": 93, "read_at": time.time() - 5}
    s = _publish(state="recording", address="AA:BB", stats=st)["stats"]
    assert s["battery_stale"] is False
    assert s["battery_age_seconds"] < 60


def test_no_reading_time_means_no_false_freshness():
    """Better to say nothing about age than to imply it is current."""
    s = _publish(state="recording", address="AA:BB",
                 stats={"battery": 50})["stats"]
    assert "battery_stale" not in s


# --- away is not broken --------------------------------------------------
# 2026-09-12: a flat battery overnight read as "401 failures", 379 of which
# were the daemon correctly noticing an absent device.

def test_an_unreachable_recorder_is_reported_as_away():
    omid.SYNC_FAIL.update({"n": 40, "last": "BleakDeviceNotFoundError",
                           "at": time.time(), "first": time.time() - 40000})
    omid.SEEN.update({"at": None, "how": None})
    out = _publish(state="connecting", address="AA:BB", stats={})
    assert out["sync_failing"]["away"] is True


def test_a_present_recorder_that_keeps_failing_is_not_away():
    omid.SYNC_FAIL.update({"n": 40, "last": "TimeoutError",
                           "at": time.time(), "first": time.time() - 4000})
    omid.SEEN.update({"at": time.time(), "how": "TimeoutError after contact"})
    out = _publish(state="recording", address="AA:BB", stats={})
    assert out["sync_failing"]["away"] is False, (
        "present-and-failing is the case that needs acting on")


# --- the radio goes back -------------------------------------------------
# 2026-09-13: a -84 dBm link gave 3.0 kB/s and a 28.9-hour ETA, and live
# capture was suspended for the whole of it because sync holds the radio.

def test_sync_takes_a_time_budget():
    import inspect
    sig = inspect.signature(omi_sync.sync)
    assert "max_seconds" in sig.parameters


def test_the_daemon_actually_passes_one():
    """A budget nobody uses is not a budget."""
    src = open(os.path.join(HERE, "..", "host", "omid.py")).read()
    body = "".join(l for l in src.splitlines(True)
                   if not l.lstrip().startswith("#"))
    assert "max_seconds=SYNC_MAX_SECONDS" in body
    assert 0 < omid.SYNC_MAX_SECONDS <= 30 * 60, (
        "a visit long enough to be useful, short enough to give the radio back")


def test_the_budget_stops_between_batches_not_mid_packet():
    """Resumability is the whole reason stopping early is safe: every batch
    is fsynced and the read pointer advanced before the next is requested."""
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    loop = src[src.index("deadline = "):src.index("return spool_path")]
    stop = loop.index("time.time() >= deadline")
    read = loop.index("await link.read(")
    assert stop < read, "the deadline must be checked before asking for more"


# --- and the radio is shared both ways -----------------------------------
# Bounding the sync was half of it. Sync runs *between* sessions and a
# session runs until the link drops, so on a healthy link sync never gets a
# turn: 12.5 hours waiting on the device, a session streaming for hours, and
# not one sync attempted. The backlog was never being asked for.

def test_a_session_yields_when_a_backlog_is_waiting():
    src = open(os.path.join(HERE, "..", "host", "omid.py")).read()
    body = "".join(l for l in src.splitlines(True)
                   if not l.lstrip().startswith("#"))
    assert "SESSION_MAX_WITH_BACKLOG" in body
    assert "client.disconnect()" in body, (
        "the only way to offer the ring a turn is to stop streaming")


def test_both_halves_of_the_radio_are_bounded():
    assert 0 < omid.SYNC_MAX_SECONDS <= 30 * 60
    assert 0 < omid.SESSION_MAX_WITH_BACKLOG <= 60 * 60


def test_a_session_is_not_cut_short_for_an_empty_ring():
    """A reconnect costs seconds of audio; not worth paying on a timer when
    there is nothing waiting."""
    assert omid.BACKLOG_WORTH_YIELDING > 0
    src = open(os.path.join(HERE, "..", "host", "omid.py")).read()
    blk = src[src.index("async def on_tick"):src.index("beat = {")]
    assert "held >" in blk, "yielding must be conditional on a real backlog"
