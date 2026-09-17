"""A hang is indistinguishable from health from the outside.

2026-09-17: the machine's Bluetooth was switched off at 13:58. The adapter's
D-Bus objects died under an await with no timeout and the retry loop sat in
select() for five hours. `Restart=always` was set on the unit and did nothing,
because the daemon stopped without stopping -- systemd saw a healthy service,
the status file froze mid-sentence, and the recorder buffered 95,484 packets
with nobody collecting them. It ignored SIGTERM and had to be killed.

So the daemon has to report its own liveness, and act on the absence of it.
"""
import os
import sys
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omid


def test_publishing_is_what_counts_as_alive():
    """Every pass of the retry loop publishes -- including a pass that finds
    nothing -- so the status write is the heartbeat already being emitted."""
    omid.ALIVE["at"] = 0
    import json
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    old = omid.STATUS
    try:
        omid.STATUS = tmp
        omid.publish(state="not found", address="AA:BB")
    finally:
        omid.STATUS = old
        os.unlink(tmp)
    assert omid.ALIVE["at"] > 0, "publish must mark the loop alive"
    assert time.time() - omid.ALIVE["at"] < 5


def test_the_window_clears_a_bounded_sync():
    """A sync visit is the longest the loop is legitimately busy, and it
    publishes progress throughout -- but the window must clear it regardless,
    or a healthy transfer would restart the service underneath itself."""
    assert omid.WATCHDOG_SILENCE > omid.SYNC_MAX_SECONDS


def test_the_window_is_not_so_long_that_a_night_is_lost():
    """Five hours was the damage. Anything on this order is a gap, not a
    night."""
    assert omid.WATCHDOG_SILENCE <= 30 * 60


def test_it_exits_hard_rather_than_cancelling():
    """A loop wedged on a dead D-Bus connection will not answer a
    cancellation either, and this has to work in exactly that case."""
    import inspect
    src = inspect.getsource(omid._watchdog)
    assert "os._exit" in src
    body = "".join(l for l in src.splitlines(True)
                   if not l.lstrip().startswith("#"))
    assert "os._exit" in body, "the hard exit must be code, not a comment"


def test_the_unit_restarts_what_the_watchdog_kills():
    """The watchdog only converts a hang into an exit. Something else has to
    bring it back, and it is worth asserting that it will."""
    unit = os.path.expanduser("~/.config/systemd/user/omid.service")
    if not os.path.exists(unit):
        unit = os.path.join(HERE, "..", "host", "omid.service")
    text = open(unit).read()
    assert "Restart=always" in text
