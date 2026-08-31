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


class TestClearingOutWhatHoldsNothing:
    """Deleting by what was heard, and the rule that makes it safe.

    A clip goes only when its ENTIRE description is things the owner called
    uninteresting. "Delete anything tagged Typing" would take the clip that is
    typing AND a dog, and the dog is the reason that clip exists -- this
    archive really does hold a turkey, a cat, and four clips of a dog barking,
    every one of them in the same silent-clip pool as seventy clips of nothing
    but a keyboard.
    """

    def _db(self, tmp_path, monkeypatch):
        import index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        return index_db

    def _add(self, db, name, sounds, has_speech=0, voice_tag=0.0):
        c = db._conn()
        c.execute("""INSERT INTO clips(name, seconds, modified, status,
                                       has_speech, edited, speakers, preview,
                                       indexed_at, voice_tag, sounds)
                     VALUES(?,30.0,1000.0,'done',?,0,'[]','',1000.0,?,?)""",
                  (name, has_speech, voice_tag,
                   "\n".join(sounds) if sounds else None))
        c.commit()

    def test_clips_are_grouped_by_their_whole_tag_set(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Typing", "Computer keyboard"])
        self._add(db, "b.wav", ["Computer keyboard", "Typing"])   # same set
        self._add(db, "c.wav", ["Typing", "Dog"])
        groups = {tuple(sorted(g["tags"])): g["clips"] for g in db.cleanup_groups()}
        assert groups[("Computer keyboard", "Typing")] == 2
        assert groups[("Dog", "Typing")] == 1

    def test_choosing_typing_does_not_take_the_dog(self, tmp_path, monkeypatch):
        """The whole reason this works by set and not by tag."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "keys.wav", ["Typing"])
        self._add(db, "dog.wav", ["Typing", "Dog"])
        picked = db.clips_for_sound_sets([["Typing"]])
        assert [n for names in picked for n in names] == ["keys.wav"]

    def test_a_voice_keeps_a_clip_out_entirely(self, tmp_path, monkeypatch):
        """Excluded at the source, so no choice made in the interface can
        reach one."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "quiet.wav", ["Typing"], voice_tag=0.4)
        assert db.cleanup_groups() == []
        assert db.clips_for_sound_sets([["Typing"]]) == []

    def test_a_clip_with_words_is_never_offered(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "spoken.wav", ["Typing"], has_speech=1)
        assert db.cleanup_groups() == []

    def test_an_unexamined_clip_is_never_offered(self, tmp_path, monkeypatch):
        """No tags means nothing has listened to it, which is not the same as
        nothing being in it."""
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "unknown.wav", [])
        assert db.cleanup_groups() == []

    def test_a_set_that_no_longer_exists_selects_nothing(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Typing"])
        assert db.clips_for_sound_sets([["Didgeridoo"]]) == []

    def test_order_of_the_chosen_tags_does_not_matter(self, tmp_path, monkeypatch):
        db = self._db(tmp_path, monkeypatch)
        self._add(db, "a.wav", ["Typing", "Computer keyboard"])
        assert db.clips_for_sound_sets([["Computer keyboard", "Typing"]]) \
            == [["a.wav"]]


class TestOverrulingASoundTag:
    """A tag that is wrong is worse than a tag that is missing.

    The tagger can be defensible and still wrong for the purpose: talking to
    a dog in a falsetto came back as "Pigeon, dove", which it genuinely sounded
    like, and which is useless when the tags are what you search with, because
    it puts a person's voice in front of you under the name of a bird.

    So a removal is recorded rather than the tag deleted -- the model's actual
    output stays, the correction survives re-transcription, and it can be undone.
    """

    def _db(self, tmp_path, monkeypatch):
        import index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        return index_db

    def _clip(self, tmp_path, monkeypatch, sounds, removed):
        """A clip on disk, indexed the way the server indexes one."""
        import json, index_db, soundfile as sf, numpy as np
        db = self._db(tmp_path, monkeypatch)
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(index_db, "DATA", str(data))
        wav = data / "c.wav"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
        (data / "transcripts" / "c.json").write_text(json.dumps({
            "clip": "c.wav", "segments": [], "sounds": sounds,
            "sounds_removed": removed}))
        db.upsert_clip("c.wav", transcript_path=str(data / "transcripts" / "c.json"),
                       wav_path=str(wav))
        return db

    def test_a_removed_tag_leaves_the_searchable_index(self, tmp_path, monkeypatch):
        db = self._clip(tmp_path, monkeypatch,
                        [["Pigeon, dove", 0.24], ["Bird", 0.24]], ["Pigeon, dove"])
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Bird"]

    def test_it_is_gone_from_the_menu_too(self, tmp_path, monkeypatch):
        db = self._clip(tmp_path, monkeypatch,
                        [["Pigeon, dove", 0.24]], ["Pigeon, dove"])
        assert db.sound_vocabulary() == []
        assert db.notable_sounds() == []

    def test_removing_a_voice_tag_changes_what_counts_as_silent(self, tmp_path, monkeypatch):
        """voice_tag decides which clips are safe to delete, so a correction
        has to reach it too -- not only the names."""
        db = self._clip(tmp_path, monkeypatch, [["Speech", 0.8]], ["Speech"])
        row = db._conn().execute("SELECT voice_tag FROM clips").fetchone()
        assert row["voice_tag"] == 0.0

    def test_nothing_removed_changes_nothing(self, tmp_path, monkeypatch):
        db = self._clip(tmp_path, monkeypatch, [["Bird", 0.30]], [])
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Bird"]

    def test_a_missing_removal_list_is_not_an_error(self, tmp_path, monkeypatch):
        """Every transcript written before this existed has no such field."""
        db = self._clip(tmp_path, monkeypatch, [["Bird", 0.30]], None)
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Bird"]


class TestSoundRowsGrewAThirdField:
    """A stored sound was [name, score]; it is now [name, score, when].

    The tagger listens in overlapping windows, so it knows roughly where in
    the clip it heard the thing. Every transcript written before that has the
    two-field form and there are fourteen hundred of them, so both shapes have
    to work everywhere -- a consumer that unpacks exactly two values raises
    ValueError on the new rows and takes the indexer down with it.
    """

    def _db(self, tmp_path, monkeypatch):
        import index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        return index_db

    def _index(self, tmp_path, monkeypatch, sounds, removed=None):
        import json, index_db, soundfile as sf, numpy as np
        db = self._db(tmp_path, monkeypatch)
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(index_db, "DATA", str(data))
        wav = data / "c.wav"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
        tp = data / "transcripts" / "c.json"
        tp.write_text(json.dumps({"clip": "c.wav", "segments": [],
                                  "sounds": sounds,
                                  "sounds_removed": removed or []}))
        db.upsert_clip("c.wav", transcript_path=str(tp), wav_path=str(wav))
        return db

    def test_the_new_three_field_rows_index(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [["Dog", 0.56, 8.0]])
        row = db._conn().execute("SELECT sounds, voice_tag FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Dog"]

    def test_the_old_two_field_rows_still_index(self, tmp_path, monkeypatch):
        """Fourteen hundred transcripts predate the third field."""
        db = self._index(tmp_path, monkeypatch, [["Dog", 0.56]])
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Dog"]

    def test_a_mixture_of_both_shapes_indexes(self, tmp_path, monkeypatch):
        """Which is what a transcript looks like mid-migration."""
        db = self._index(tmp_path, monkeypatch,
                         [["Dog", 0.56, 8.0], ["Speech", 0.66]])
        row = db._conn().execute("SELECT sounds, voice_tag FROM clips").fetchone()
        assert set((row["sounds"] or "").split("\n")) == {"Dog", "Speech"}
        assert row["voice_tag"] == 0.66

    def test_a_removal_still_applies_to_a_timed_row(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch,
                         [["Pigeon, dove", 0.24, 3.0], ["Bird", 0.24, 3.0]],
                         removed=["Pigeon, dove"])
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Bird"]

    def test_an_empty_row_does_not_take_the_indexer_down(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [[], ["Dog", 0.56, 8.0]])
        row = db._conn().execute("SELECT sounds FROM clips").fetchone()
        assert (row["sounds"] or "").split("\n") == ["Dog"]


class TestSureVersusMerelyPossible:
    """Two bars, because browsing and searching want different ones.

    624 clips carry Vehicle and four of them clear 0.35: the rest is this
    machine's fan being mistaken for a distant engine. A browsing list built
    from the low bar is mostly that mistake; a filter built from it can still
    turn up the real ones, and a filter is a question rather than a claim.
    """

    def _index(self, tmp_path, monkeypatch, sounds):
        import json, index_db, soundfile as sf, numpy as np
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(index_db, "DATA", str(data))
        wav = data / "c.wav"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
        tp = data / "transcripts" / "c.json"
        tp.write_text(json.dumps({"clip": "c.wav", "segments": [],
                                  "sounds": sounds, "sounds_removed": []}))
        index_db.upsert_clip("c.wav", transcript_path=str(tp), wav_path=str(wav))
        return index_db

    def test_a_weak_tag_is_searchable_but_not_browsable(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [["Vehicle", 0.22, 4.0]])
        assert [r["name"] for r in db.sound_vocabulary()] == ["Vehicle"]
        assert db.notable_sounds() == [], "0.22 is not a claim that a car went by"

    def test_a_confident_tag_reaches_both(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [["Dog", 0.56, 8.0]])
        assert [r["name"] for r in db.sound_vocabulary()] == ["Dog"]
        assert [g["tag"] for g in db.notable_sounds()] == ["Dog"]

    def test_the_fan_wearing_a_stethoscope_is_room_noise(self, tmp_path, monkeypatch):
        """75 clips claimed a heartbeat; 70 of them also had Hum and 55 a fan.
        There is no heartbeat."""
        db = self._index(tmp_path, monkeypatch,
                         [["Heart sounds, heartbeat", 0.55, 20.0],
                          ["Heart murmur", 0.40, 20.0],
                          ["Throbbing", 0.45, 20.0]])
        assert db.notable_sounds() == []

    def test_the_column_is_added_to_an_existing_database(self, tmp_path, monkeypatch):
        import sqlite3, index_db
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        index_db._conn()
        c = sqlite3.connect(str(tmp_path / "index.db"))
        cols = {r[1] for r in c.execute("PRAGMA table_info(clips)")}
        c.close()
        assert "sounds_strong" in cols


class TestTheFanKeepsGettingANewName:
    """The same rushing air, named three different things by the same model.

    At five-second windows it was a heartbeat: 75 clips, 70 of them also Hum,
    43 also Heart murmur. Widening the windows cured that and produced 218
    clips of Vehicle, 189 also Aircraft, the most confident recorded at 00:55
    and 05:45. Played back, both are a fan blowing into a microphone.

    The device's owner, hearing it: "it did sound a bit like an aircraft engine
    close up". That is the difficulty in one line -- the model is not being
    stupid, and the tag is still useless, because a tag is for finding things.

    Hidden from browsing, kept searchable: a real car remains findable by
    asking for it, and cannot fill a page titled "what is in this archive".
    """

    def _index(self, tmp_path, monkeypatch, sounds):
        import json, index_db, soundfile as sf, numpy as np
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(index_db, "DATA", str(data))
        wav = data / "c.wav"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
        tp = data / "transcripts" / "c.json"
        tp.write_text(json.dumps({"clip": "c.wav", "segments": [],
                                  "sounds": sounds, "sounds_removed": []}))
        index_db.upsert_clip("c.wav", transcript_path=str(tp), wav_path=str(wav))
        return index_db

    def test_the_engine_costume_is_not_browsable(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch,
                         [["Vehicle", 0.58, 5.0], ["Aircraft", 0.46, 5.0]])
        assert db.notable_sounds() == []

    def test_but_it_is_still_searchable(self, tmp_path, monkeypatch):
        """A real car has to stay findable by someone who asks for one."""
        db = self._index(tmp_path, monkeypatch, [["Vehicle", 0.58, 5.0]])
        assert [r["name"] for r in db.sound_vocabulary()] == ["Vehicle"]

    def test_a_dog_in_the_same_clip_still_shows(self, tmp_path, monkeypatch):
        """Suppressing the fan must not suppress what happened beside it."""
        db = self._index(tmp_path, monkeypatch,
                         [["Vehicle", 0.58, 5.0], ["Dog", 0.57, 8.0]])
        assert [g["tag"] for g in db.notable_sounds()] == ["Dog"]


class TestSoundsThatAreRightAndWronglyNamed:
    """Two different kinds of wrong, needing two different treatments.

    Played to the person who was there: both Patter clips were the fan and both
    Arrow clips were the fan or a television, so those are hidden like the
    other three fan costumes. But both Typewriter clips were real -- his dog's
    toenails on the kitchen linoleum, one of them while he talked about treats.

    Suppressing that would throw away a genuine signal about the room. Leaving
    it alone would put a typewriter in a house that has none. So the label
    stays, because it is what the model said and what the filter searches for,
    and what it actually is travels beside it -- to the page, and through the
    MCP to whatever reads this archive later.
    """

    def _index(self, tmp_path, monkeypatch, sounds):
        import json, index_db, soundfile as sf, numpy as np
        monkeypatch.setattr(index_db, "DB_PATH", str(tmp_path / "index.db"))
        monkeypatch.setattr(index_db, "_local", type(index_db._local)())
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(index_db, "DATA", str(data))
        wav = data / "c.wav"
        sf.write(str(wav), np.zeros(16000, dtype="float32"), 16000)
        tp = data / "transcripts" / "c.json"
        tp.write_text(json.dumps({"clip": "c.wav", "segments": [],
                                  "sounds": sounds, "sounds_removed": []}))
        index_db.upsert_clip("c.wav", transcript_path=str(tp), wav_path=str(wav))
        return index_db

    def test_the_fourth_fan_costume_is_hidden(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch,
                         [["Patter", 0.60, 0.0], ["Arrow", 0.67, 0.0]])
        assert db.notable_sounds() == []

    def test_a_real_sound_with_a_wrong_name_is_kept(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [["Typewriter", 0.72, 0.0]])
        g = db.notable_sounds()
        assert [x["tag"] for x in g] == ["Typewriter"]
        assert g[0]["really"] == "a dog's claws on a hard floor"

    def test_an_ordinary_sound_carries_no_correction(self, tmp_path, monkeypatch):
        db = self._index(tmp_path, monkeypatch, [["Dog", 0.56, 8.0]])
        assert db.notable_sounds()[0]["really"] is None

    def test_the_wrongly_named_one_is_still_searchable_by_its_name(
            self, tmp_path, monkeypatch):
        """The filter searches what the model said, not what it meant."""
        db = self._index(tmp_path, monkeypatch, [["Typewriter", 0.72, 0.0]])
        assert [r["name"] for r in db.sound_vocabulary()] == ["Typewriter"]


class TestLinesWithNobodyBehindThem:
    """A transcript line the diarizer could not attribute.

    In the clip view these rendered no label element at all -- an unexplained
    gap where every other line carries a name, and no way to put one there,
    because the only clickable label was the one that did not exist. Reported
    as "some lines have no name label at all, or none that is visible".

    Not a rare glitch either: it is what distant speech looks like from here,
    a television or someone through a doorway, and those clips are exactly the
    ones worth labelling by hand.
    """

    def _page(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            return f.read()

    def test_a_line_without_a_speaker_still_gets_a_label(self):
        page = self._page()
        i = page.index("const who = s.speaker")
        block = page[i:i + 700]
        assert '"no voice matched"' in block, \
            "an unattributed line must say so rather than render nothing"
        assert 'wholine noone' in block

    def test_the_empty_string_branch_is_gone(self):
        """The old code ended that ternary with "", which is the bug."""
        page = self._page()
        i = page.index("const who = s.speaker")
        block = page[i:i + 700]
        # the ternary's else-branch must produce a button, not nothing
        assert "\n      : \"\";" not in block

    def test_a_pinned_name_wins_over_the_placeholder(self):
        """Once a name is pinned to the line it does have an answer."""
        page = self._page()
        i = page.index("const who = s.speaker")
        block = page[i:i + 700]
        assert "s.speaker_name || \"no voice matched\"" in block

    def test_it_opens_the_line_editor_rather_than_the_voice_namer(self):
        """There is no voice to rename; the name goes on the line."""
        page = self._page()
        i = page.index('if (w && !s.speaker){')
        block = page[i:i + 500]
        assert "editLine(" in block
        assert "nameSpeaker(" not in block


class TestAnswersThatAreNotAName:
    """What to put on a line of television.

    Pinning a name to a line never touches the voiceprint store -- an embedding
    describes a whole diarized cluster, not one line -- so it is the right
    mechanism for "this came out of a screen". The weakness was free text: TV,
    tv and Television are three labels to anything reading the archive later,
    and the archive is meant to be read by something that cannot ask.

    So the empty field offers the answers that are not a person, written the
    same way every time.
    """

    def _page(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            return f.read()

    def test_the_non_person_answers_exist(self):
        page = self._page()
        i = page.index("const NOT_A_PERSON")
        block = page[i:i + 400]
        for label in ('"Media"', '"Someone else"', '"Not speech"'):
            assert label in block

    def test_they_are_offered_when_the_field_is_empty(self):
        """That is when someone has no name to give, which is the whole case."""
        page = self._page()
        i = page.index("function renderNameSuggestions")
        block = page[i:i + 1400]
        assert "NOT_A_PERSON" in block
        assert "if (!t){" in block

    def test_typing_still_narrows_to_real_people(self):
        page = self._page()
        i = page.index("function renderNameSuggestions")
        block = page[i:i + 1400]
        assert "namerPeople.filter" in block

    def test_pinning_never_enrols_a_voiceprint(self):
        """The property that makes this safe to put on a television.

        Asserted against the code rather than against its comment: the first
        version of this checked for a sentence in the docstring, which wraps,
        so it failed while the behaviour it described was correct. A test that
        reads prose is testing the wrong thing anyway.
        """
        import os, re
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "server.py"),
                  encoding="utf-8") as f:
            server = f.read()
        i = server.index('if "speaker_name" in body:')
        # up to the end of that handler
        block = server[i:server.index("@app.delete", i)]
        code = "\n".join(l for l in block.splitlines()
                          if not l.strip().startswith("#"))
        for enrol in ("speaker_store", "add_voiceprint", "save_speaker",
                      "ingest_unknown", "name_person"):
            assert enrol not in code, f"pinning a line must not call {enrol}"


class TestTheLabelsThatAreNotPeople:
    """"Media", "Someone else", "Not speech" — offered in two places, enforced
    in a third.

    Naming a voice enrols a voiceprint, unlike pinning a line, so a person
    called Media would be built out of every screen and radio voice in the
    house and would sit in the reference set competing to name real people. The server marks
    these media on the way through: still matched, so the same television is
    not asked about twice, and capped at "uncertain" so it can never put its
    label on anybody by itself.

    The spelling is the contract. "Media" typed as "TV" is a different
    person and none of the enforcement applies, so the browser's list and the
    server's list must agree, and this is what says so.
    """

    def _files(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "server.py"), encoding="utf-8") as f:
            server = f.read()
        with open(os.path.join(here, "..", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            page = f.read()
        return server, page

    def test_the_two_lists_are_the_same(self):
        import re
        server, page = self._files()
        s = re.search(r"NOT_A_PERSON_NAMES = \(([^)]*)\)", server).group(1)
        srv = set(re.findall(r'"([^"]+)"', s))
        i = page.index("const NOT_A_PERSON")
        ui = set(re.findall(r'\["([^"]+)",', page[i:i + 400]))
        assert srv == ui, f"server has {srv}, the page offers {ui}"

    def test_the_server_marks_them_media(self):
        server, _ = self._files()
        i = server.index("if name in NOT_A_PERSON_NAMES:")
        block = server[i:i + 400]
        assert "set_kind" in block and "KIND_MEDIA" in block

    def test_they_are_offered_in_both_places(self):
        _, page = self._files()
        # the line editor's empty-field branch, and the voice namer's chips
        assert page.count("NOT_A_PERSON") >= 3


class TestWhichNonPersonLabelsLearn:
    """Three answers, and only one of them should teach the matcher anything.

    Described by the person using it: "Someone else would be somebody talking
    that is not important, and Not speech would be for things that were not
    speech". Both were being enrolled, which contradicts each label in a
    different way — "not worth identifying" followed by building a reference
    for them, and a voiceprint of something that is not a voice sitting in the
    reference set being compared against real people.

    TV is learned on purpose: a television is a real recurring voice, so
    matching it again is what stops the same one being asked about twice.
    """

    def _server(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "server.py"), encoding="utf-8") as f:
            return f.read()

    def test_two_of_the_three_never_enrol(self):
        import re
        s = self._server()
        block = re.search(r"NEVER_ENROL_NAMES = \(([^)]*)\)", s).group(1)
        assert set(re.findall(r'"([^"]+)"', block)) == {"Someone else", "Not speech"}

    def test_media_is_deliberately_not_in_that_list(self):
        """A television is a real recurring voice; matching it again is the
        point of learning it."""
        import re
        s = self._server()
        block = re.search(r"NEVER_ENROL_NAMES = \(([^)]*)\)", s).group(1)
        assert "Media" not in re.findall(r'"([^"]+)"', block)

    def test_the_gate_runs_before_the_voiceprint_is_looked_at(self):
        """It must refuse by name, not by whether a vector happens to exist.

        Measured on offsets in the whole file rather than inside a fixed-size
        window: the first version sliced 900 characters and the explanatory
        comment between the two branches is longer than that, so it failed on
        the size of a comment rather than on the order of the code.
        """
        s = self._server()
        start = s.index("enrolled, reason, count = False, None, None")
        gate = s.index("if name in NEVER_ENROL_NAMES:", start)
        novec = s.index("elif vec is None:", start)
        assert gate < novec, "the name check must come first"

    def test_all_three_are_still_marked_media(self):
        s = self._server()
        i = s.index("if name in NOT_A_PERSON_NAMES:")
        assert "KIND_MEDIA" in s[i:i + 400]


class TestNamingVoicesInBulk:
    """Confirming a queue full of the same channel, without going too far.

    After enrolling a YouTuber, most of the queue becomes that channel again
    below the bar that would have named it automatically -- 18 of 40 entries in
    one case. Confirming those one at a time is the work the enrolment was
    supposed to remove.

    The floor is the point. The weakest entry in that filtered list scored
    0.29, which the matcher itself would not act on, so a bulk action offering
    to name it is offering to be wrong eighteen times at once.
    """

    def _page(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            return f.read()

    def test_the_bulk_floor_matches_the_store(self):
        """It must not name anything the matcher would refuse to name alone."""
        import re, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
        import speaker_store
        page = self._page()
        ui = float(re.search(r"const BULK_NAME_FLOOR = ([0-9.]+)", page).group(1))
        assert ui == speaker_store.MATCH_LOW, \
            f"the page bulk-names from {ui}, the store's floor is {speaker_store.MATCH_LOW}"

    def test_the_weak_ones_are_excluded_not_hidden(self):
        """They stay in the list to be listened to; only the button skips them."""
        page = self._page()
        i = page.index("function syncVoiceNear")
        block = page[i:i + 1400]
        assert "too uncertain to name in bulk" in block
        assert "left \n" not in block

    def test_the_action_filters_by_the_same_floor(self):
        page = self._page()
        i = page.index('on("bVoiceConfirmAll", "click"')
        block = page[i:i + 400]
        assert "BULK_NAME_FLOOR" in block

    def test_it_takes_two_presses(self):
        page = self._page()
        i = page.index('on("bVoiceConfirmAll", "click"')
        block = page[i:i + 900]
        assert "voiceConfirmArmed" in block and "Really name" in block


class TestASlotWithSomebodyElsePinnedInIt:
    """Naming a voice must not learn from audio a listener has said is not
    theirs.

    The impurity check catches a slot the diarizer itself found incoherent. It
    cannot catch this one: diarization was confident, one voice, and a person
    listening disagreed about part of it and said so by pinning a name.

    On the clip that exposed it, nine lines were Ryan Long and one was pinned
    Danny Polishchuk. Naming the slot enrolled 29.7 seconds as Ryan with
    Danny's five inside it -- and re-diarizing the audio afterwards found two
    speakers in it, the louder matching Danny at 0.87.
    """

    def _f(self):
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
        import server
        return server.names_pinned_elsewhere

    def test_a_foreign_pin_is_reported(self):
        segs = [{"speaker": "SPEAKER_00", "speaker_name": None},
                {"speaker": "SPEAKER_00", "speaker_name": "Danny Polishchuk"}]
        assert self._f()(segs, "SPEAKER_00", "Ryan Long") == ["Danny Polishchuk"]

    def test_a_pin_agreeing_with_the_name_is_not(self):
        segs = [{"speaker": "SPEAKER_00", "speaker_name": "Ryan Long"}]
        assert self._f()(segs, "SPEAKER_00", "Ryan Long") == []

    def test_pins_in_another_slot_are_not_this_slot_s_problem(self):
        segs = [{"speaker": "SPEAKER_01", "speaker_name": "Danny Polishchuk"}]
        assert self._f()(segs, "SPEAKER_00", "Ryan Long") == []

    def test_several_are_listed_once_and_in_order(self):
        segs = [{"speaker": "S", "speaker_name": "Zoe"},
                {"speaker": "S", "speaker_name": "Amy"},
                {"speaker": "S", "speaker_name": "Amy"}]
        assert self._f()(segs, "S", "Ryan Long") == ["Amy", "Zoe"]

    def test_an_empty_transcript_is_not_an_error(self):
        assert self._f()([], "SPEAKER_00", "Ryan Long") == []
        assert self._f()(None, "SPEAKER_00", "Ryan Long") == []

    def test_the_name_still_applies_only_the_learning_stops(self):
        """The distinction the store is built on: a label is correctable, a
        reference is not."""
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "server.py"), encoding="utf-8") as f:
            server = f.read()
        i = server.index("elif pinned_elsewhere:")
        block = server[i:i + 500]
        assert "The name is applied" in block
        assert "Split by voice" in block


class TestRescuingASlotThatWillNotSplit:
    """A suspect slot is not two voices, and refusing it cost 38% of the archive.

    The purity check flags slots whose turns disagree. Its own docstring says
    what a flag means: not two people -- splitting refused every one it was
    given, 2849 of them here, at every cluster count tried. What it actually
    measures is unknown.

    Meanwhile 25 voices had every voiceprint flagged, so they could be named
    but never taught, and they held 62 of 163 minutes: a narrator saying
    "welcome back to Cody's Lab" 34 times among them.

    The turns that agree with each other are still that voice. Pooling those
    and leaving out the ones that disagree gives a reference that cannot be a
    blend of two people, which is the only thing the flag ever protected
    against.
    """

    def _source(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "pipeline.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_a_refused_split_reports_the_core(self):
        src = self._source()
        for reason in ('"one side collapsed"', '"no clean separation"'):
            i = src.index(reason)
            assert "core" in src[i - 40:i + 160], f"{reason} must carry the core"

    def test_the_core_is_turns_that_agree_with_the_medoid(self):
        src = self._source()
        i = src.index("medoid = int(np.argmax(S.sum(axis=1)))")
        block = src[i:i + 200]
        assert "S[medoid, i]) >= self.CORE_MIN" in block

    def test_the_core_bar_is_below_the_bar_for_joining_two_slots(self):
        """Turns seconds apart in one recording are a weaker question than two
        slots being the same person."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
        import pipeline, speaker_store
        assert pipeline.Worker.CORE_MIN < speaker_store.CLUSTER_MIN

    def test_a_rescued_slot_stops_being_suspect(self):
        """It is no longer a pooled blend, so it no longer carries the warning
        that it might be one."""
        src = self._source()
        start = src.index("core_vecs[(wi, spk)]")
        end = src.index("continue", start)          # the end of the branch
        block = src[start:end]
        assert '"suspect"] = False' in block
        assert '"rescued"] = True' in block

    def test_it_refuses_when_there_is_too_little_agreeing_speech(self):
        src = self._source()
        i = src.index("if len(core) >= 2 and secs >= self.CORE_MIN_SECONDS:")
        assert i > 0

    def test_the_core_vector_is_preferred_over_the_pooled_one(self):
        """pyannote's vector was pooled over the disagreeing turns too."""
        src = self._source()
        i = src.index("vec = split_vecs.get((wi, spk))")
        block = src[i:i + 260]
        assert block.index("core_vecs.get") < block.index("local.get(spk)")


class TestOfferingTheRestOfTheClip:
    """After naming one line, offer the others rather than doing them.

    A name put on a single line is usually true of more than that line -- a
    video running across the end of a clip, a second person speaking for a
    stretch -- and doing them one at a time is the same name typed five times
    with five chances to type it differently.

    Offered, not done: the reason a name went on ONE line is that the others
    were somebody else, and assuming otherwise would undo the correction just
    made.
    """

    def _page(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "static", "index.html"),
                  encoding="utf-8") as f:
            return f.read()

    def test_it_offers_rather_than_acts(self):
        page = self._page()
        i = page.index("async function offerTheRestOfTheClip")
        end = page.index("/* Downloads are blocked", i)
        block = page[i:end]
        assert "no, just that one" in block
        assert "name ${others.length" in block

    def test_it_says_how_many_and_which_name(self):
        page = self._page()
        i = page.index("async function offerTheRestOfTheClip")
        block = page[i:page.index("/* Downloads are blocked", i)]
        assert "other line" in block and "${pinned}" in block

    def test_it_skips_lines_that_already_carry_that_name(self):
        page = self._page()
        i = page.index("async function offerTheRestOfTheClip")
        block = page[i:page.index("/* Downloads are blocked", i)]
        assert '(s.speaker_name || "") !== pinned' in block

    def test_it_only_appears_when_a_name_was_pinned(self):
        page = self._page()
        assert "if (pinned) offerTheRestOfTheClip(" in page

    def test_nothing_scrolls_smoothly(self):
        """A smooth scroll does not animate in a tab that is not visible, so
        the confirmation stays below the fold -- which is the bug the scroll
        was added to fix. Measured: smooth left it at y=1015 in a 929 px
        window, instant put it at 826."""
        page = self._page()
        assert 'behavior: "smooth"' not in page


class TestRenamingReachesTheRecordings:
    """A rename in the store has to change what the clips say.

    The transcripts hold the name as a copy -- it is what the reader shows and
    what search and export use -- so renaming somebody in the store alone left
    every clip still saying the old thing, with the store and the archive
    disagreeing about who somebody is.

    Re-matching does not cover it: that leaves hand-set names alone by design,
    which is right, and means a correction to one of those would never land.
    """

    def _rename(self, tmp_path, monkeypatch, transcripts):
        import json, sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
        import server, pipeline, index_db
        data = tmp_path / "data"
        (data / "transcripts").mkdir(parents=True)
        monkeypatch.setattr(server, "DATA", str(data))
        monkeypatch.setattr(pipeline, "DATA", str(data))
        monkeypatch.setattr(pipeline, "TRANSCRIPTS", str(data / "transcripts"))
        monkeypatch.setattr(index_db, "upsert_clip", lambda *a, **k: None)
        for name, body in transcripts.items():
            (data / name).write_bytes(b"")
            (data / "transcripts" / (name[:-4] + ".json")).write_text(json.dumps(body))
        return server, data

    def test_a_resolved_name_is_carried_over(self, tmp_path, monkeypatch):
        server, data = self._rename(tmp_path, monkeypatch, {
            "a.wav": {"speakers": {"SPEAKER_00": {"name": "Ryan Long"}},
                      "segments": []}})
        import json
        assert server._rename_in_transcripts("Ryan Long", "Ryan") == 1
        t = json.loads((data / "transcripts" / "a.json").read_text())
        assert t["speakers"]["SPEAKER_00"]["name"] == "Ryan"

    def test_a_pinned_line_is_carried_over(self, tmp_path, monkeypatch):
        server, data = self._rename(tmp_path, monkeypatch, {
            "a.wav": {"speakers": {}, "segments":
                      [{"text": "hi", "speaker_name": "Ryan Long"}]}})
        import json
        assert server._rename_in_transcripts("Ryan Long", "Ryan") == 1
        t = json.loads((data / "transcripts" / "a.json").read_text())
        assert t["segments"][0]["speaker_name"] == "Ryan"

    def test_candidates_are_carried_over_too(self, tmp_path, monkeypatch):
        """Otherwise the queue keeps offering a name nobody has any more."""
        server, data = self._rename(tmp_path, monkeypatch, {
            "a.wav": {"speakers": {"S": {"name": None, "candidates":
                                         [{"name": "Ryan Long", "score": 0.7}]}},
                      "segments": []}})
        import json
        assert server._rename_in_transcripts("Ryan Long", "Ryan") == 1
        t = json.loads((data / "transcripts" / "a.json").read_text())
        assert t["speakers"]["S"]["candidates"][0]["name"] == "Ryan"

    def test_other_people_are_left_alone(self, tmp_path, monkeypatch):
        server, data = self._rename(tmp_path, monkeypatch, {
            "a.wav": {"speakers": {"S": {"name": "Nathan"}}, "segments": []}})
        assert server._rename_in_transcripts("Ryan Long", "Ryan") == 0

    def test_it_counts_clips_not_mentions(self, tmp_path, monkeypatch):
        server, _ = self._rename(tmp_path, monkeypatch, {
            "a.wav": {"speakers": {"S": {"name": "Ryan Long"}},
                      "segments": [{"text": "x", "speaker_name": "Ryan Long"}]},
            "b.wav": {"speakers": {"S": {"name": "Ryan Long"}}, "segments": []}})
        assert server._rename_in_transcripts("Ryan Long", "Ryan") == 2


class TestSmallSlotsGetACoreToo:
    """Splitting needs six turns to mean anything; a core needs two.

    One test was doing both jobs, so _split_slot returned "too few usable
    turns" before computing anything -- and 1258 slots were left with no
    reference at all when three turns of which two agree is a perfectly good
    one. The split is still refused below SPLIT_MIN_TURNS; the core is what
    gets salvaged from the refusal.
    """

    def _source(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "pipeline.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_the_early_return_is_now_at_two_turns(self):
        src = self._source()
        i = src.index("n = len(vecs)")
        block = src[i:src.index("M = np.stack(vecs)", i)]
        assert "if n < 2:" in block
        assert "SPLIT_MIN_TURNS" not in block, \
            "the split threshold must not gate the core"

    def test_a_small_slot_still_refuses_to_split(self):
        src = self._source()
        i = src.index("if n < self.SPLIT_MIN_TURNS:")
        block = src[i:src.index("\n\n", i)]
        assert '"split": False' in block
        assert '"too few usable turns"' in block

    def test_but_it_carries_the_core_out(self):
        src = self._source()
        i = src.index("if n < self.SPLIT_MIN_TURNS:")
        block = src[i:src.index("\n\n", i)]
        assert '"core": core' in block and '"medoid": medoid' in block


class TestTheCoreAlwaysExists:
    """An absolute similarity bar fails the slots that need rescuing most.

    A narrator whose turns agree at 0.297 -- poor far-field audio, and 1.8
    second turns are near the limit of what the embedder can do -- has almost
    nothing clearing 0.60, so the core came back empty and the slot stayed
    unusable. That was the case the rescue existed for and the one it refused:
    two voices holding 17 of the 28 remaining locked-out minutes.
    """

    def _source(self):
        import os
        here = os.path.dirname(__file__)
        with open(os.path.join(here, "..", "web", "pipeline.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_the_absolute_bar_is_tried_first(self):
        src = self._source()
        i = src.index("medoid = int(np.argmax(S.sum(axis=1)))")
        block = src[i:src.index("if n < self.SPLIT_MIN_TURNS:", i)]
        assert "self.CORE_MIN" in block
        assert block.index("self.CORE_MIN") < block.index("if len(core) < 2")

    def test_it_falls_back_to_the_agreeing_half(self):
        src = self._source()
        i = src.index("if len(core) < 2 and n >= 2:")
        block = src[i:src.index("if n < self.SPLIT_MIN_TURNS:", i)]
        assert "-float(S[medoid, i])" in block, "sorted by likeness to the medoid"
        assert "n // 2" in block

    def test_it_is_still_a_subset_and_not_everything(self):
        """Taking all the turns would be the pooled vector again, which is what
        this exists to avoid."""
        src = self._source()
        i = src.index("if len(core) < 2 and n >= 2:")
        block = src[i:src.index("if n < self.SPLIT_MIN_TURNS:", i)]
        assert "order[:max(2, n // 2)]" in block
