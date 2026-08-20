"""Regressions in the web layer that no board is needed to catch.

Every test here corresponds to a fault that was found in review and shown to
be real before it was fixed.
"""
import os
import sqlite3
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import agent_runner
import index_db


class TestFtsQuery:
    """A quotation mark in the search box used to be a 500."""

    @pytest.mark.parametrize("typed", [
        'he said "hi"', "don't", 'a OR b', "x*", "NEAR(a b)", '"', '""',
        "ünïcödé", "a" * 500,
    ])
    def test_survives_sqlite(self, typed):
        q = index_db.fts_query(typed)
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.execute("INSERT INTO t VALUES (?)", (typed,))
        if q:                      # empty input legitimately yields no query
            c.execute("SELECT * FROM t WHERE t MATCH ?", (q,)).fetchall()

    def test_finds_the_words_that_were_typed(self):
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.execute("INSERT INTO t VALUES (?)", ('she said "hello" clearly',))
        q = index_db.fts_query('said "hello"')
        assert len(c.execute("SELECT * FROM t WHERE t MATCH ?", (q,)).fetchall()) == 1

    def test_operators_are_searched_not_executed(self):
        """'a OR b' must not match a document containing only 'a'."""
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.execute("INSERT INTO t VALUES (?)", ("a",))
        q = index_db.fts_query("a OR b")
        assert c.execute("SELECT * FROM t WHERE t MATCH ?", (q,)).fetchall() == []

    def test_blank_input_yields_no_query(self):
        assert index_db.fts_query("   ") == ""


class TestAgentKind:
    """`kind` arrives from a query string and was joined straight into a path."""

    @pytest.mark.parametrize("bad", [
        "../../etc/passwd", "tasks/../../x", "../agent/tasks",
        "nope", "", "tasks.jsonl", "/etc/passwd",
    ])
    def test_rejected(self, bad):
        with pytest.raises(ValueError):
            agent_runner._kinds(bad)

    @pytest.mark.parametrize("good", ["tasks", "events", "notes", "facts"])
    def test_accepted(self, good):
        assert agent_runner._kinds(good) == [good]

    def test_none_means_all(self):
        assert agent_runner._kinds(None) == list(agent_runner.KINDS)

    def test_store_path_stays_in_the_store(self):
        for k in agent_runner.KINDS:
            p = agent_runner._store_path(k)
            assert os.path.dirname(p) == os.path.abspath(agent_runner.STORE)


class TestPeakLevel:
    """abs() on int16 leaves the most negative sample negative."""

    def test_full_scale_negative_reads_full_scale(self):
        pcm = np.array([-32768, 0, 5], dtype=np.int16)
        assert int(np.abs(pcm.astype(np.int32)).max()) == 32768

    def test_the_old_way_was_wrong(self):
        """Documents the bug, so nobody reintroduces the narrow version."""
        pcm = np.array([-32768], dtype=np.int16)
        assert int(np.abs(pcm).max()) < 0


class TestInfoContract:
    """Both firmware layouts, through the parser the device actually uses.

    The two builds disagree about bytes 13-26 and about which optional fields
    exist. Every bug this has produced was the host reading one layout as if
    it were the other, and none of them were visible without a board until
    this test existed.
    """

    CAP_STEPS, CAP_TAP_DIAG, CAP_OVERRUNS, CAP_STATE = 0x0001, 0x0010, 0x0020, 0x0040

    def _frame(self, fw, caps, *, streaming=False, rate16=True):
        info = bytearray(40)
        info[1] = 1 if rate16 else 0
        info[5] = (4 if streaming else 0)
        info[6] = 1                                  # IMU present
        info[18], info[19] = 1, fw
        info[20], info[21] = caps & 0xFF, caps >> 8
        return info

    def zephyr(self, **kw):
        caps = self.CAP_STEPS | 0x0002 | 0x0004 | 0x0008 | self.CAP_STATE
        return self._frame(2, caps, **kw), caps

    def arduino(self, **kw):
        caps = (self.CAP_TAP_DIAG | 0x0004 | self.CAP_OVERRUNS | self.CAP_STATE)
        return self._frame(1, caps, **kw), caps

    def test_firmware_identity(self):
        from server import parse_info
        assert parse_info(self.zephyr()[0])["firmware"] == "zephyr"
        assert parse_info(self.arduino()[0])["firmware"] == "arduino"

    def test_steps_only_where_the_bytes_are_steps(self):
        """Byte 13 is a step count on one build and a tap counter on the other."""
        from server import parse_info
        z, _ = self.zephyr()
        a, _ = self.arduino()
        z[13], a[13] = 200, 200
        assert parse_info(z)["steps"] == 200
        assert "steps" not in parse_info(a)

    def test_motion_flags_suppressed_on_arduino(self):
        from server import parse_info
        a, _ = self.arduino()
        a[17] = 0xFF                       # tap diagnostics, not motion flags
        p = parse_info(a)
        assert p["tilt"] is None and p["moving"] is None

    def test_overruns_only_where_counted(self):
        from server import parse_info
        z, a = self.zephyr()[0], self.arduino()[0]
        z[38] = a[38] = 7
        assert parse_info(a)["ring_overruns"] == 7
        assert parse_info(z)["ring_overruns"] is None

    def test_capture_state_is_reported_by_both(self):
        from server import parse_info
        for build in (self.zephyr, self.arduino):
            assert parse_info(build(streaming=True)[0])["device_streaming"] is True
            assert parse_info(build(streaming=False)[0])["device_streaming"] is False

    def test_firmware_without_the_state_bit_is_unknown_not_stopped(self):
        """The bug: absent was read as 'not capturing', so the host re-armed
        every second, and Arduino's stream command drops the ring."""
        from server import parse_info
        info = self._frame(1, self.CAP_TAP_DIAG)      # no CAP_STATE
        assert parse_info(info)["device_streaming"] is None

    def test_short_frames_do_not_raise(self):
        from server import parse_info
        for n in range(0, 41):
            parse_info(bytearray(n))
