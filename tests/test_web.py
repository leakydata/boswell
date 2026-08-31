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


class TestClipNameValidation:
    """Eight endpoints each had their own version of this and they had drifted."""

    @pytest.mark.parametrize("bad", [
        "../../etc/passwd.wav", "sub/x.wav", "/abs/x.wav", "x.mp3",
        "", ".wav.", "x.wav/", "..", "x.WAV",
    ])
    def test_rejected(self, bad):
        from fastapi import HTTPException
        import server
        with pytest.raises(HTTPException):
            server.safe_clip(bad)

    @pytest.mark.parametrize("good", ["clip_1.wav", "recovered_2.wav", "a.wav"])
    def test_accepted_and_lands_in_data(self, good):
        import server
        p = server.safe_clip(good)
        assert os.path.dirname(os.path.abspath(p)) == os.path.abspath(server.DATA)

    def test_transcript_path_requires_a_bare_name(self):
        import pipeline
        for bad in ("../x.wav", "sub/x.wav", "/abs/x.wav", ""):
            with pytest.raises(ValueError):
                pipeline.transcript_path(bad)

    def test_transcript_path_still_works_for_real_names(self):
        import pipeline
        assert pipeline.transcript_path("clip_1.wav").endswith("clip_1.json")


class TestBootId:
    """Frame timestamps restart at zero on reboot; the host must notice."""

    CAP_BOOTID = 0x0080

    def _frame(self, boot_id, with_cap=True):
        info = bytearray(40)
        info[18], info[19] = 1, 2
        caps = self.CAP_BOOTID if with_cap else 0
        info[20], info[21] = caps & 0xFF, caps >> 8
        info[22], info[23] = boot_id & 0xFF, boot_id >> 8
        return info

    def test_boot_id_is_read(self):
        from server import parse_info
        assert parse_info(self._frame(49246))["boot_id"] == 49246

    def test_absent_capability_reports_none(self):
        """Firmware that does not publish it must not look like boot id 0."""
        from server import parse_info
        assert parse_info(self._frame(1234, with_cap=False))["boot_id"] is None

    def test_a_reboot_changes_it(self):
        """The two values observed on real hardware across a reboot."""
        from server import parse_info
        before = parse_info(self._frame(49246))["boot_id"]
        after = parse_info(self._frame(44996))["boot_id"]
        assert before != after

    def test_short_frame_has_no_boot_id(self):
        from server import parse_info
        assert parse_info(bytearray(22))["boot_id"] is None


class TestVocabularyTermination:
    """apply_vocabulary ran inside the transcription worker and could not stop.

    Its phrase pass reran while any window matched, and a multi-word term
    already spelled correctly matches itself: "Ryan Long" joins to "ryanlong",
    hits the term "Ryan Long", is replaced by the identical string, and the loop
    goes round again. Every multi-word term did it -- and enrolled names are
    added to the vocabulary automatically, so naming somebody with a first and
    last name armed it. The first clip transcribing that name would have stopped
    the worker for good.
    """

    def _apply(self, terms, text, seconds=5):
        import signal

        import pipeline

        def bang(sig, frame):
            raise TimeoutError("apply_vocabulary did not terminate")

        old = signal.signal(signal.SIGALRM, bang)
        signal.alarm(seconds)
        try:
            return pipeline.apply_vocabulary(text, terms)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)

    @pytest.mark.parametrize("term,text", [
        ("Ryan Long", "ryan long ago"),
        ("Ryan Long", "Ryan Long"),
        ("Peggy Gironde", "peggy gironde said"),
        ("NileRed YouTube", "nilered youtube video"),
        ("Data Slayer YouTube", "the data slayer youtube channel"),
    ])
    def test_a_term_already_spelled_correctly_terminates(self, term, text):
        assert self._apply([term], text)

    def test_it_still_rejoins_a_split_term(self):
        assert self._apply(["Metformin"], "metformen tablets") == "Metformin tablets"
        assert self._apply(["ADPCM"], "adp cm measurement") == "ADPCM measurement"

    def test_a_term_is_normalised_but_only_once(self):
        assert self._apply(["Ryan Long"], "ryan long ago") == "Ryan Long ago"

    def test_a_three_word_term_rejoins(self):
        """The adjacency check took the stretch from the first token's end to
        the last token's start, which for a three-word window contains the
        middle word -- so it always found letters there and always skipped.
        Width-3 rejoining had never fired, which is most of the multi-word
        terms."""
        t = "Data Slayer YouTube"
        assert self._apply([t], "data slayer youtube channel") == f"{t} channel"

    @pytest.mark.parametrize("text", [
        "the boss well knows",
        "ryan longed for it",
        "we had an ell of cloth",
        "network truck driver",
        "data slaying dragons",
    ])
    def test_ordinary_phrases_survive(self, text):
        """A wrong rejoin does not mis-spell a word, it deletes one: this pass
        turned "the boss well knows" into "the Boswell knows" and "ryan longed
        for it" into "Ryan Long for it". It matches letters exactly now."""
        terms = ["Boswell", "Ryan Long", "Eli", "Network Chuck YouTube",
                 "Data Slayer YouTube", "Nathan"]
        assert self._apply(terms, text) == text


class TestHallucinatedSilence:
    """Whisper writes a stock phrase over room tone. The diarizer is the check:
    no voice anywhere means nobody spoke, whatever the decoder produced.

    The rule lived inside _process and nowhere else, so the consolidation and
    re-transcription passes -- which rewrite the same field -- put back 24 of
    these that the original transcription had removed.
    """

    def _f(self, segs):
        import pipeline
        return pipeline.is_hallucinated_silence(segs)

    def test_a_stock_phrase_with_no_speaker_is_silence(self):
        assert self._f([{"text": "Thank you.", "speaker": None}])
        assert self._f([{"text": "Okay.", "speaker": None}])
        assert self._f([{"text": ". . . . . .", "speaker": None}])

    def test_a_short_utterance_with_a_speaker_is_speech(self):
        assert not self._f([{"text": "Yeah.", "speaker": "SPEAKER_00"}])

    def test_real_speech_survives_even_without_a_speaker(self):
        long = {"text": "x" * 100, "speaker": None}
        assert not self._f([long])

    def test_nothing_is_not_silence_to_discard(self):
        assert not self._f([])


class TestCaptureReconciliation:
    """The device and the host can disagree about whether it is recording.

    Only one direction was handled -- host armed, device silent -- so a device
    that kept capturing after being told to stop was never told again. Observed
    live: prefs armed=False, the button reading "Start capture", the firmware's
    magenta LED (which it defines as armed and buffering to flash), and clips
    landing every thirty seconds throughout.
    """

    def _r(self, streaming, armed):
        import server
        return server.reconcile_capture(streaming, armed)

    def test_device_silent_while_armed_is_re_armed(self):
        assert self._r(False, True) == "rearm"

    def test_device_capturing_while_paused_is_stopped_again(self):
        """The direction that was missing, and the one that matters more.
        Failing to record loses a conversation; recording while the interface
        says otherwise breaks a promise to whoever is in the room."""
        assert self._r(True, False) == "redisarm"

    def test_agreement_needs_no_correction(self):
        assert self._r(True, True) is None
        assert self._r(False, False) is None

    def test_a_device_that_does_not_report_is_not_guessed_at(self):
        assert self._r(None, True) is None
        assert self._r(None, False) is None


class TestImpureNeverEnrolls:
    """Naming a blended slot must apply the name and learn nothing.

    A slot flagged suspect at coherence 0.472 -- 26s of the wearer and 4s of a
    video pooled into one vector -- was enrolled as Nathan and again as NileRed
    YouTube, twice each: four byte-identical references describing two people,
    sitting in both reference sets. The impure gate covered origin "auto" and
    left the hand-naming path open, which is the one a person actually uses.
    """

    def test_the_store_refuses_an_impure_automatic_reference(self, tmp_path, monkeypatch):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "web"))
        import numpy as np
        import speaker_store as store
        monkeypatch.setattr(store, "DB", str(tmp_path / "s.db"))
        store._cache.update(version=None, ids=None, pids=None, M=None)
        store._bump()

        rng = np.random.default_rng(1)
        v = store.unit(rng.normal(size=256))
        pid = store.person_id_for("Alice")
        assert not store.add_voiceprint(pid, v, origin="auto", impure=True)["ok"]
        # An unnamed cluster may still hold it -- it claims nothing about who
        # anybody is, and that is what keeps it reachable in the queue.
        cluster = store.new_person()
        assert store.add_voiceprint(cluster, v, origin="auto", impure=True)["ok"]


class TestQuietClipsAreLifted:
    """The gain applied before transcription, and its refusals.

    A quiet recording is the ordinary case for a device worn on a shirt, and
    measured on 60 clips that already transcribe, lifting one gains words on
    the quiet ones (+12 mean below -30 dBFS) and does nothing to the loud ones
    (+1.0). The refusals matter as much as the lift: it must never attenuate
    audio that is already loud, and must never scale digital silence, which
    would raise a noise floor into the decoder's hearing and get a stock
    phrase written over a room where nobody spoke.
    """

    def test_a_quiet_clip_is_brought_up(self):
        import pipeline
        # 0.1 peak needs 8.9x, inside the cap. A tenth of this would need 89x
        # and come back capped instead, which is the next test.
        a = (np.sin(np.linspace(0, 400, 16000)) * 0.1).astype(np.float32)
        assert abs(float(np.abs(pipeline.normalise(a)).max())
                   - pipeline.ASR_PEAK) < 1e-3

    def test_a_loud_clip_is_left_exactly_alone(self):
        import pipeline
        a = (np.sin(np.linspace(0, 400, 16000)) * 0.95).astype(np.float32)
        assert pipeline.normalise(a) is a

    def test_silence_is_not_scaled_into_noise(self):
        import pipeline
        a = np.zeros(16000, dtype=np.float32)
        assert pipeline.normalise(a) is a

    def test_the_gain_is_capped(self):
        """Below the cap there is only noise floor, and amplifying that invites
        the decoder to write something over a room where nobody spoke."""
        import pipeline
        a = (np.sin(np.linspace(0, 400, 16000)) * 0.001).astype(np.float32)
        out = pipeline.normalise(a)
        peak = float(np.abs(out).max())
        assert peak < pipeline.ASR_PEAK, "must not reach full scale from noise"
        assert abs(peak - 0.001 * pipeline.ASR_MAX_GAIN) < 1e-3

    def test_it_is_one_scalar_over_the_whole_clip(self):
        """Per-part gain would invent a level difference the room never had."""
        import pipeline
        a = np.concatenate([np.full(8000, 0.01),
                            np.full(8000, 0.0005)]).astype(np.float32)
        out = pipeline.normalise(a)
        assert abs(float(out[:8000].max() / out[8000:].max())
                   - float(a[:8000].max() / a[8000:].max())) < 1e-3

    def test_empty_input_does_not_raise(self):
        import pipeline
        assert len(pipeline.normalise(np.zeros(0, dtype=np.float32))) == 0


class TestTheSilenceGuardUsesTheRightEvidence:
    """What the guard is allowed to delete, and what it is not.

    It exists because the decoder writes "Thank you." over room tone, and the
    diarizer is the check on that. But it used to read segment labels rather
    than the diarizer's own findings, and those are different things: a label
    lands on a segment only if assign_word_speakers overlapped a diarization
    turn with it, and when the timings disagree the label goes missing while
    the voice is still there. A clip of two people talking over a fan lost the
    line "I have to go to the toilet right now." that way -- 37 characters,
    under the limit, no label, and a voiceprint for SPEAKER_00 in the very
    same transcript.
    """

    def test_a_stock_phrase_over_room_tone_is_still_removed(self):
        import pipeline
        segs = [{"text": "Thank you.", "speaker": None}]
        assert pipeline.is_hallucinated_silence(segs, {}) is True

    def test_a_found_voice_saves_a_short_line_with_no_label(self):
        import pipeline
        segs = [{"text": "I have to go to the toilet right now.", "speaker": None}]
        assert len(segs[0]["text"]) <= pipeline.HALLUCINATION_CHARS
        assert pipeline.is_hallucinated_silence(segs, {"SPEAKER_00": [0.1]}) is False

    def test_a_labelled_segment_survives_regardless(self):
        import pipeline
        segs = [{"text": "Yeah.", "speaker": "SPEAKER_00"}]
        assert pipeline.is_hallucinated_silence(segs, {}) is False

    def test_long_text_is_never_touched(self):
        import pipeline
        segs = [{"text": "x" * (pipeline.HALLUCINATION_CHARS + 1), "speaker": None}]
        assert pipeline.is_hallucinated_silence(segs, {}) is False

    def test_no_segments_is_not_a_hallucination(self):
        import pipeline
        assert pipeline.is_hallucinated_silence([], {}) is False

    def test_omitting_the_evidence_keeps_the_old_behaviour(self):
        """Callers that cannot supply it must not silently stop guarding."""
        import pipeline
        segs = [{"text": "Okay.", "speaker": None}]
        assert pipeline.is_hallucinated_silence(segs) is True


class TestTheMissedVoiceQueue:
    """Clips the sound tagger heard a voice in and the transcriber did not.

    This is a list a person works through by hand, so what it must never do is
    waste their time: the archive holds 1273 silent clips whose loudest tag is
    white noise, a fan or a keyboard, and a queue that included those would be
    unusable. It is also the only place the two models' disagreement is
    visible at all.
    """

    def _db(self, tmp_path, monkeypatch):
        import index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        return index_db

    def _add(self, db, name, has_speech, voice_tag):
        c = db._conn()
        c.execute("""INSERT INTO clips(name, seconds, modified, status,
                                       has_speech, edited, speakers, preview,
                                       indexed_at, voice_tag)
                     VALUES(?,30.0,1000.0,'done',?,0,'[]','',1000.0,?)""",
                  (name, has_speech, voice_tag))
        c.commit()

    def test_a_silent_clip_with_a_voice_is_listed(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", 0, 0.63)
        assert [r["name"] for r in db.missed_voice()] == ["a.wav"]

    def test_a_clip_that_transcribed_is_not(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", 1, 0.90)
        assert db.missed_voice() == []

    def test_fan_and_keyboard_stay_out(self, tmp_path, monkeypatch):
        """The 1273-clip majority. Below the floor there is nothing to hear."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "fan.wav", 0, 0.01)
        self._add(db, "keys.wav", 0, 0.0)
        assert db.missed_voice() == []

    def test_untagged_clips_stay_out(self, tmp_path, monkeypatch):
        """NULL means never examined, which is not the same as no voice."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", 0, None)
        assert db.missed_voice() == []

    def test_the_most_promising_come_first(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "faint.wav", 0, 0.10)
        self._add(db, "clear.wav", 0, 0.78)
        assert [r["name"] for r in db.missed_voice()] == ["clear.wav", "faint.wav"]

    def test_the_column_is_added_to_an_existing_database(self, tmp_path, monkeypatch):
        """It arrived after the table did; an old index must not break."""
        import sqlite3
        db = self._db(tmp_path, monkeypatch)
        db._conn()                       # create at current schema
        c = sqlite3.connect(str(tmp_path / "index.db"))
        cols = {r[1] for r in c.execute("PRAGMA table_info(clips)")}
        c.close()
        assert "voice_tag" in cols


class TestTheTailOfARecording:
    """A segment cannot begin where the audio ends.

    Whisper writes one there anyway on the last window. Measured across 4693
    segments in 1154 clips: dropping those that start within 0.10 s of the end
    removes 9 segments, the longest 10 characters -- "Thank you." five times,
    "you" twice, "No." once. At 0.25 s it would take 28, the longest 87
    characters, and real sentences start appearing. So the margin is small on
    purpose.
    """

    def test_a_segment_at_the_very_end_is_dropped(self):
        import pipeline
        segs = [{"start": 30.0, "end": 30.0, "text": "Thank you."}]
        assert pipeline.drop_tail_segments(segs, 30.06) == []

    def test_ordinary_speech_is_kept(self):
        import pipeline
        segs = [{"start": 12.0, "end": 14.0, "text": "Good Lord, that motor."}]
        assert len(pipeline.drop_tail_segments(segs, 30.0)) == 1

    def test_a_line_just_inside_the_margin_survives(self):
        import pipeline
        segs = [{"start": 29.5, "end": 30.0, "text": "Yeah."}]
        assert len(pipeline.drop_tail_segments(segs, 30.0)) == 1

    def test_an_unknown_duration_changes_nothing(self):
        import pipeline
        segs = [{"start": 30.0, "end": 30.0, "text": "Thank you."}]
        assert pipeline.drop_tail_segments(segs, 0) == segs
        assert pipeline.drop_tail_segments(segs, None) == segs


class TestUnattributedSpeech:
    """A voice was found, and not one line could be given to it.

    Measured: five clips confirmed by the person in the recording as a
    television or someone on the phone through an open door are 100%
    unlabelled, and of the 1090 other clips the conversation pass has been
    over, not one is. So this is a marker for distant speech, and it matters
    because the words in such a transcript are part real and part guessed --
    "good lord" and "motor" were genuinely said; the sentence around them was
    the decoder's.
    """

    def test_a_voice_with_no_line_attached_is_marked(self):
        import pipeline
        segs = [{"text": "Good Lord, shoot that thing's motor.", "speaker": None}]
        assert pipeline.unattributed(segs, {"SPEAKER_00": [0.1]}) is True

    def test_one_attached_line_is_enough_to_clear_it(self):
        import pipeline
        segs = [{"text": "a", "speaker": "SPEAKER_00"}, {"text": "b", "speaker": None}]
        assert pipeline.unattributed(segs, {"SPEAKER_00": [0.1]}) is False

    def test_no_voice_at_all_is_a_different_thing(self):
        """That is silence, and is_hallucinated_silence's business, not this."""
        import pipeline
        segs = [{"text": "Thank you.", "speaker": None}]
        assert pipeline.unattributed(segs, {}) is False

    def test_a_clip_with_no_words_is_not_marked(self):
        import pipeline
        assert pipeline.unattributed([], {"SPEAKER_00": [0.1]}) is False
        assert pipeline.unattributed([{"text": "  ", "speaker": None}],
                                     {"SPEAKER_00": [0.1]}) is False

    def test_it_accepts_plain_evidence_too(self):
        """The consolidation path knows only whether a turn overlapped."""
        import pipeline
        segs = [{"text": "distant words", "speaker": None}]
        assert pipeline.unattributed(segs, True) is True
        assert pipeline.unattributed(segs, False) is False


class TestWhatElseWasHeard:
    """The sound tags, as something you can search rather than something the
    archive merely knows.

    A second model listens to every clip and names what it hears from 527
    everyday classes. It had been running on all of them for weeks with
    nowhere to appear, which is indistinguishable from not running at all --
    and it is what tells a silent clip from an empty one.
    """

    def _db(self, tmp_path, monkeypatch):
        import index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        return index_db

    def _add(self, db, name, sounds):
        c = db._conn()
        c.execute("""INSERT INTO clips(name, seconds, modified, status,
                                       has_speech, edited, speakers, preview,
                                       indexed_at, sounds)
                     VALUES(?,30.0,1000.0,'done',1,0,'[]','',1000.0,?)""",
                  (name, "\n".join(sounds) if sounds else None))
        c.commit()

    def test_the_menu_comes_from_the_archive(self, tmp_path, monkeypatch):
        """Not from AudioSet's 527 classes. A menu offering Didgeridoo to
        somebody whose recordings hold a dog and a keyboard is unusable."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Dog", "Speech"])
        self._add(db, "b.wav", ["Speech"])
        names = [r["name"] for r in db.sound_vocabulary()]
        assert names == ["Speech", "Dog"], "commonest first"
        assert "Didgeridoo" not in names

    def test_it_counts_clips_not_occurrences(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Dog"])
        self._add(db, "b.wav", ["Dog"])
        assert db.sound_vocabulary()[0] == {"name": "Dog", "clips": 2}

    def test_a_clip_with_no_tags_is_not_an_empty_name(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", [])
        assert db.sound_vocabulary() == []

    def test_the_names_survive_a_round_trip(self, tmp_path, monkeypatch):
        """Stored newline-joined; a two-word class must not become two."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Domestic animals, pets", "Computer keyboard"])
        assert {r["name"] for r in db.sound_vocabulary()} == {
            "Domestic animals, pets", "Computer keyboard"}

    def test_the_column_is_added_to_an_existing_database(self, tmp_path, monkeypatch):
        import sqlite3
        db = self._db(tmp_path, monkeypatch)
        db._conn()
        c = sqlite3.connect(str(tmp_path / "index.db"))
        cols = {r[1] for r in c.execute("PRAGMA table_info(clips)")}
        c.close()
        assert "sounds" in cols
