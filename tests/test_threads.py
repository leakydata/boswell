"""Putting a thought back together after a clip cut it in half.

A thirty-second clip is a transport unit and it cuts wherever thirty seconds
landed, so one sentence arrives as two rows. These cover the join that undoes
that, and the sectioning that goes on top of it.
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import threads


def seg(clip, index, at, until, text, speaker="SPEAKER_00", name=None,
        shaky=False, device_id="d1"):
    return {"clip": clip, "index": index, "start": 0.0, "end": until - at,
            "at": at, "until": until, "text": text, "speaker": speaker,
            "name": name, "shaky": shaky, "device_id": device_id}


# ------------------------------------------------------------------ stitching
def test_a_sentence_split_by_a_clip_boundary_comes_back_together():
    """The whole point. Thirty seconds elapses mid-sentence, the recorder
    files what it has, and the rest arrives as the next clip's first line."""
    # Neither slot has a name -- half this archive's lines do not -- so the
    # join has to be decided on how the two slots sound.
    voices = {("a.wav", "SPEAKER_00"): [1.0, 0.0, 0.0],
              ("b.wav", "SPEAKER_02"): [0.95, 0.31, 0.0]}
    out = threads.stitch([
        seg("a.wav", 0, 100.0, 129.0, "I was going to say that the whole"),
        seg("b.wav", 0, 129.4, 132.0, "point of it is the timing.",
            speaker="SPEAKER_02"),
    ], voices=voices)
    assert len(out) == 1
    assert out[0]["text"] == ("I was going to say that the whole "
                              "point of it is the timing.")
    assert out[0]["lines"] == 2
    # And the pieces survive, so a reader can still reach the audio for
    # either half and an edit still lands on the line it belongs to.
    assert [p["clip"] for p in out[0]["parts"]] == ["a.wav", "b.wav"]


def test_a_name_learned_late_names_the_whole_run():
    # The voice did not change; only what was known about it did.
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "One."),
        seg("a.wav", 1, 3.2, 6.0, "Two.", name="Nathan"),
    ])
    assert len(out) == 1 and out[0]["name"] == "Nathan"


def test_a_real_pause_starts_a_new_utterance():
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "Are you coming?"),
        seg("a.wav", 1, 9.0, 11.0, "Right, I will get my coat."),
    ])
    assert len(out) == 2, "a nine-second gap was joined as one thought"


def test_two_speakers_are_never_one_utterance():
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "Are you coming?", speaker="SPEAKER_00"),
        seg("a.wav", 1, 3.1, 5.0, "In a minute.", speaker="SPEAKER_01"),
    ])
    assert len(out) == 2


def test_an_id_does_not_carry_across_a_clip():
    """SPEAKER_00 here is not SPEAKER_00 in the next clip. The server already
    learned this by merging the maps and putting one person's name on three
    other people's speech; joining on it here would do the same silently."""
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "Mine.", speaker="SPEAKER_00"),
        seg("b.wav", 0, 3.2, 5.0, "Not mine.", speaker="SPEAKER_00"),
    ])
    assert len(out) == 2, "an unnamed id was trusted across a clip boundary"


def test_the_same_named_person_does_carry_across_a_clip():
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "Mine.", speaker="SPEAKER_00", name="Nathan"),
        seg("b.wav", 0, 3.2, 5.0, "Still mine.", speaker="SPEAKER_03",
            name="Nathan"),
    ])
    assert len(out) == 1, "a resolved name should carry across clips"


def test_a_shaky_attribution_ends_the_run():
    # A slot the diarizer could not hold together is not a basis for deciding
    # two lines are one person speaking.
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "One."),
        seg("a.wav", 1, 3.1, 5.0, "Two.", shaky=True),
    ])
    assert len(out) == 2


def test_two_recorders_are_two_rooms():
    out = threads.stitch([
        seg("a.wav", 0, 0.0, 3.0, "Here.", name="Nathan", device_id="omi"),
        seg("b.wav", 0, 3.1, 5.0, "There.", name="Nathan", device_id="boswell"),
    ])
    assert len(out) == 2, "audio from two microphones was spliced together"


def test_one_person_talking_forever_still_gets_handles():
    # Without a cap this becomes a wall of text, which is the problem being
    # fixed rather than the fix.
    segs, t = [], 0.0
    for i in range(200):
        segs.append(seg("a.wav", i, t, t + 2.0, "word " * 5, name="Nathan"))
        t += 2.1
    out = threads.stitch(segs)
    assert len(out) > 1
    assert all(u["seconds"] <= threads.MAX_SECONDS + 3 for u in out)
    assert all(u["words"] <= threads.MAX_WORDS + 5 for u in out)


def test_lines_with_no_shared_timeline_are_left_alone():
    """A clip whose start is unknown keeps its offsets, and an offset from one
    file compared against an offset from another reads a 28-second gap as if
    the second line came first."""
    a = seg("a.wav", 0, 0.0, 3.0, "One.")
    b = seg("b.wav", 0, 0.0, 3.0, "Two.", name="Nathan")
    a["at"] = a["until"] = None
    out = threads.stitch([a, b])
    assert len(out) == 2


def test_absolute_times_come_from_the_clip_they_belong_to():
    segs = [{"clip": "a.wav", "index": 0, "start": 3.0, "end": 6.0, "text": "x"},
            {"clip": "b.wav", "index": 0, "start": 1.0, "end": 2.0, "text": "y"}]
    out = threads.with_absolute_times(segs, {"a.wav": 1000.0, "b.wav": 1030.0})
    assert out[0]["at"] == 1003.0 and out[0]["until"] == 1006.0
    assert out[1]["at"] == 1031.0
    # A clip with no known start is not guessed at.
    out2 = threads.with_absolute_times(segs, {"a.wav": 1000.0})
    assert out2[1]["at"] is None


# ----------------------------------------------------------------- sectioning
def _unit(at, until, vec, name="Nathan", clip="a.wav", index=0):
    return {"clip": clip, "index": index, "at": at, "until": until,
            "text": "some words here", "name": name, "seconds": until - at,
            "words": 3, "parts": [{"clip": clip, "index": index}]}


def _v(*xs):
    n = math.sqrt(sum(x * x for x in xs)) or 1.0
    return [x / n for x in xs]


def test_a_long_silence_is_a_boundary_whatever_the_words_did():
    """Three minutes of nothing is a different sitting. No amount of
    similarity across it means the two halves belong together."""
    v = _v(1.0, 0.0, 0.0)
    units, vecs, t = [], {}, 0.0
    for i in range(6):
        if i == 3:
            t += 600.0                       # ten minutes of nothing
        units.append(_unit(t, t + 5.0, v, index=i))
        vecs[("a.wav", i)] = v
        t += 6.0
    secs = threads.sections(units, vecs)
    assert len(secs) == 2, "a ten-minute silence did not start a new section"
    assert secs[1]["why"] == "silence"


def test_a_change_of_subject_is_a_boundary():
    # Two clearly different directions, joined in the middle.
    one, two = _v(1.0, 0.0, 0.0), _v(0.0, 1.0, 0.0)
    units, vecs, t = [], {}, 0.0
    for i in range(12):
        v = one if i < 6 else two
        units.append(_unit(t, t + 5.0, v, index=i))
        vecs[("a.wav", i)] = v
        t += 6.0
    secs = threads.sections(units, vecs)
    assert len(secs) >= 2, "a complete change of subject was not noticed"
    assert any(s["why"] == "subject" for s in secs[1:])
    # The break lands where the subject actually changed.
    first = len(secs[0]["units"])
    assert 4 <= first <= 8, f"boundary at {first}, expected near 6"


def test_one_steady_subject_is_left_as_one_section():
    """A conversation that stays on one thing is uniformly similar, and a
    fixed threshold would cut it to ribbons. Depth scoring is what stops
    that."""
    units, vecs, t = [], {}, 0.0
    for i in range(12):
        v = _v(1.0, 0.02 * ((i % 3) - 1), 0.01)     # small wobble, one subject
        units.append(_unit(t, t + 5.0, v, index=i))
        vecs[("a.wav", i)] = v
        t += 6.0
    secs = threads.sections(units, vecs)
    assert len(secs) == 1, f"one subject was cut into {len(secs)} sections"


def test_units_with_no_vector_do_not_invent_a_boundary():
    # A third of the archive's lines are under 25 characters and never
    # embedded. Absent is not low.
    units, vecs, t = [], {}, 0.0
    for i in range(8):
        units.append(_unit(t, t + 5.0, None, index=i))
        t += 6.0
    secs = threads.sections(units, vecs)
    assert len(secs) == 1


def test_a_unit_is_scored_by_the_lines_inside_it_that_have_vectors():
    u = _unit(0.0, 5.0, None)
    u["parts"] = [{"clip": "a.wav", "index": 0}, {"clip": "a.wav", "index": 1}]
    got = threads._unit_vector(u, {("a.wav", 1): _v(0.0, 1.0, 0.0)})
    assert got is not None, "one embedded line in the unit should be enough"
    assert abs(math.sqrt(sum(x * x for x in got)) - 1.0) < 1e-6


def test_nothing_in_gives_nothing_back():
    assert threads.stitch([]) == []
    assert threads.sections([], {}) == []


# ------------------------------------------------------- reading the archive
def test_reading_transcripts_does_not_load_the_machinery_that_wrote_them():
    """transcript_path lives in pipeline, and importing pipeline pulls in
    torch -- twelve seconds and a CUDA probe to learn a filename. A read-only
    pass, run per request by the interface, must not pay that."""
    import subprocess, sys as _sys
    here = os.path.join(os.path.dirname(__file__), "..", "web")
    out = subprocess.run(
        [_sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import threads; "
         "print('torch' in sys.modules)" % here],
        capture_output=True, text=True, timeout=120)
    assert out.stdout.strip() == "False", \
        f"importing threads pulled in torch: {out.stdout} {out.stderr[:200]}"


def test_a_clip_name_cannot_reach_outside_the_store():
    # The names come from a request body.
    for bad in ("../secrets.json", "a/b.wav", "/etc/passwd", ""):
        try:
            threads._transcript_path(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted as a clip name")
    assert threads._transcript_path("omi_1.wav").endswith("omi_1.json")


def test_the_interface_has_a_way_to_ask_for_this():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert '@app.post("/api/conversation/threads")' in src
    fn = src[src.index('@app.post("/api/conversation/threads")'):]
    fn = fn[:fn.index("\n@app.")]
    # Names from a request body are checked before they reach the disk.
    assert "safe_clip(name)" in fn
    # Off the event loop: a second of work would otherwise stall every other
    # request on the server.
    assert "run_in_executor" in fn


def test_the_depth_floor_is_set_against_real_conversation():
    """0.08 was calibrated against synthetic vectors in the tests above, and
    real speech is far noisier. Measured over this archive -- 3,852 gaps
    across 62 conversations -- the median depth is 0.047 and p95 is 0.316, so
    0.08 called 38% of all gaps a change of subject: one section every 1.3
    minutes, and a single exchange about a noise cut into three.
    """
    assert threads.MIN_DEPTH >= 0.24, \
        "the floor is back below the noise in real conversation"
    # And not so high that nothing is ever a boundary: p99 was 0.476.
    assert threads.MIN_DEPTH <= 0.45


def test_a_short_exchange_on_one_subject_stays_one_section():
    """The real failure at 0.08: three consecutive lines about one noise --
    "What is that sound?", "High-pitched, like, cicada sound.", "I think it's
    coming from the air conditioner." -- became three sections. Neighbouring
    sentences on one subject wobble; that is what conversation sounds like,
    not a change of subject.

    The wobble here is chosen to sit in the band where the old floor and the
    measured one disagree, so this fails if the floor ever goes back.
    """
    units, vecs, t = [], {}, 0.0
    for i in range(10):
        v = _v(1.0, 0.25 * ((i % 4) - 1.5), 0.05)
        units.append(_unit(t, t + 4.0, v, index=i))
        vecs[("a.wav", i)] = v
        t += 5.0
    assert len(threads.sections(units, vecs, min_depth=0.08)) > 1, \
        "this data no longer exercises the regression it was written for"
    secs = threads.sections(units, vecs)
    assert len(secs) == 1, f"one subject was cut into {len(secs)} sections"


def test_a_real_change_of_subject_still_cuts():
    # The floor must not be so high that nothing is ever a boundary.
    one, two = _v(1.0, 0.0, 0.0), _v(0.0, 1.0, 0.0)
    units, vecs, t = [], {}, 0.0
    for i in range(12):
        v = one if i < 6 else two
        units.append(_unit(t, t + 4.0, v, index=i))
        vecs[("a.wav", i)] = v
        t += 5.0
    assert len(threads.sections(units, vecs)) >= 2
