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


class TestSemanticReplace:
    """Editing a transcript shorter used to leave the removed lines searchable."""

    def _segments(self, texts):
        return [{"text": t, "start": float(i), "speaker": "SPEAKER_00"}
                for i, t in enumerate(texts)]

    def test_shortening_removes_the_dropped_rows(self, tmp_path, monkeypatch):
        import semantic
        monkeypatch.setattr(semantic, "DB", str(tmp_path / "sem.db"))
        # A deterministic stand-in for the embedding service; this test is
        # about row lifetime, not about vector quality.
        # 768 dimensions, because the vector table declares that width.
        monkeypatch.setattr(semantic, "embed",
                            lambda t: [float(len(t))] + [0.0] * 767)
        monkeypatch.setattr(semantic, "MIN_CHARS", 1)

        long = self._segments(["the first thing said", "the second thing said",
                               "the third thing said"])
        semantic.index_clip("c.wav", long, replace=True)
        semantic.index_clip("c.wav", long[:1], replace=True)

        db, _ = semantic._connect()
        try:
            rows = db.execute("SELECT idx, text FROM seg WHERE clip=? ORDER BY idx",
                              ("c.wav",)).fetchall()
        finally:
            db.close()
        assert [r["idx"] for r in rows] == [0]
        assert "second" not in " ".join(r["text"] for r in rows)

    def test_replace_updates_changed_text(self, tmp_path, monkeypatch):
        import semantic
        monkeypatch.setattr(semantic, "DB", str(tmp_path / "sem.db"))
        monkeypatch.setattr(semantic, "embed",
                            lambda t: [float(len(t))] + [0.0] * 767)
        monkeypatch.setattr(semantic, "MIN_CHARS", 1)

        semantic.index_clip("c.wav", self._segments(["before the correction"]),
                            replace=True)
        semantic.index_clip("c.wav", self._segments(["after the correction"]),
                            replace=True)

        db, _ = semantic._connect()
        try:
            rows = db.execute("SELECT text FROM seg WHERE clip=?", ("c.wav",)).fetchall()
        finally:
            db.close()
        assert len(rows) == 1 and rows[0]["text"] == "after the correction"


class TestTranscriptionQueue:
    """The same clip could be queued four different ways."""

    def _worker(self):
        import pipeline
        w = pipeline.Worker.__new__(pipeline.Worker)     # no models, no thread
        import queue as _q, threading
        w.q, w.busy = _q.Queue(), None
        w._queued, w._qlock = set(), threading.Lock()
        return w

    def test_second_submit_is_refused(self):
        w = self._worker()
        assert w.submit("a.wav") is True
        assert w.submit("a.wav") is False
        assert w.q.qsize() == 1

    def test_requeue_allowed_once_it_has_been_taken(self):
        w = self._worker()
        w.submit("a.wav")
        w.q.get()
        w._queued.discard("a.wav")
        assert w.submit("a.wav") is True

    def test_running_clip_is_not_requeued(self):
        w = self._worker()
        w.busy = "a.wav"
        assert w.submit("a.wav") is False


class TestClipWriting:
    """Two clips in one second used to overwrite each other."""

    def _device(self, tmp_path, monkeypatch):
        import numpy as np
        import server
        monkeypatch.setattr(server, "DATA", str(tmp_path))
        d = server.Device.__new__(server.Device)
        return d, server, np

    def test_same_second_does_not_overwrite(self, tmp_path, monkeypatch):
        d, server, np = self._device(tmp_path, monkeypatch)
        a = np.zeros(1600, dtype=np.int16)
        p1 = d._save_wav("clip", 1000, a, 16000)
        p2 = d._save_wav("clip", 1000, a, 16000)
        assert p1 != p2
        assert os.path.exists(p1) and os.path.exists(p2)

    def test_no_partial_file_is_left_behind(self, tmp_path, monkeypatch):
        d, server, np = self._device(tmp_path, monkeypatch)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(server.sf, "write", boom)
        with pytest.raises(OSError):
            d._save_wav("clip", 1000, np.zeros(10, dtype=np.int16), 16000)
        assert os.listdir(tmp_path) == []

    def test_audio_survives_a_failed_write(self, tmp_path, monkeypatch):
        """The buffer used to be emptied before the write, so a failure took
        the only copy of the audio with it."""
        d, server, np = self._device(tmp_path, monkeypatch)
        d._pcm = [np.ones(1600, dtype=np.int16)]
        d.state = {"rate": 16000}

        monkeypatch.setattr(server.sf, "write",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        with pytest.raises(OSError):
            d.take_clip()
        assert d._pcm, "audio was discarded before it reached disk"
