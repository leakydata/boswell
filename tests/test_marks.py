"""A press on the recorder, turned into something readable.

The window is not a guess. Measured on a real test: taps at 00:04:44,
00:04:49 and 00:04:54, and every word spoken 4 to 27 seconds *before* the
first of them, nothing after. Reaching for a button is what people do once
the thought has landed.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import marks


def read_file(p):
    with open(os.path.join(HERE, "..", p)) as f:
        return f.read()


def unit(at, until, text):
    return {"clip": "a.wav", "at": at, "until": until, "text": text}


def test_the_window_reaches_back_further_than_forward():
    """A window that only looks forward captured nothing at all on the real
    test, and an empty excerpt is indistinguishable from a broken feature."""
    assert marks.BEFORE > marks.AFTER
    assert marks.BEFORE >= 45, "27 seconds back was needed to reach the first line"
    assert marks.AFTER > 0, "sometimes the press comes first and it costs nothing"


def test_a_gesture_means_something():
    assert marks.KINDS["tap"] == "bookmark"
    assert marks.KINDS["double tap"] == "reminder"
    # A long press is the firmware powering itself off, not a mark.
    assert "long press" not in marks.KINDS


def test_only_gestures_become_marks():
    got = marks.load(os.path.join(HERE, "does-not-exist.jsonl"))
    assert got == [], "a missing log is not an error"


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"at": 100, "kind": "tap"}\n' + "not json\n"
                 + '{"at": 200, "kind": "double tap"}\n' + "\n")
    got = marks.load(str(p))
    assert [m["kind"] for m in got] == ["tap", "double tap"]


def test_the_log_comes_back_in_order(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"at": 300, "kind": "tap"}\n{"at": 100, "kind": "tap"}\n')
    assert [m["at"] for m in marks.load(str(p))] == [100, 300]


def test_the_excerpt_stops_at_a_real_silence():
    """Otherwise it runs to wherever the window happened to fall and picks up
    somebody else's sentence."""
    at = 1000.0
    us = [unit(at - 55, at - 53, "much earlier, a different thought"),
          unit(at - 8, at - 6, "the thing"),
          unit(at - 5, at - 3, "I want to remember")]
    kept = marks._trim_to_pause(us, at)
    assert [u["text"] for u in kept] == ["the thing", "I want to remember"], \
        "a 45-second gap was treated as the same run of speech"


def test_speech_after_the_press_is_kept_too():
    at = 1000.0
    us = [unit(at + 2, at + 4, "remind me to call the dentist")]
    kept = marks._trim_to_pause(us, at)
    assert len(kept) == 1, "a press followed by speech captured nothing"


def test_the_nearest_speech_is_what_the_press_was_about():
    at = 1000.0
    us = [unit(at - 3, at - 1, "this one"),
          unit(at + 40, at + 42, "unrelated, much later")]
    kept = marks._trim_to_pause(us, at)
    assert kept[0]["text"] == "this one"
    assert all(u["text"] != "unrelated, much later" for u in kept)


def test_nothing_nearby_is_not_a_crash():
    assert marks._trim_to_pause([], 1000.0) == []


def test_a_mark_says_where_the_speech_sat_around_the_press():
    """So nobody has to assume whether it came before or after -- which is
    the assumption that would have made this capture nothing."""
    src = read_file("web/marks.py")
    assert '"starts_at"' in src
    fn = src[src.index("def around("):]
    assert 'kept[0]["at"] - at' in fn


def test_the_anchor_is_the_time_not_the_clip():
    """The clip boundary fell three seconds before the first press, so the
    taps landed in a different clip from every word they were about."""
    src = read_file("web/marks.py")
    fn = src[src.index("def _candidate_clips("):]
    fn = fn[:fn.index("\ndef ")]
    # Clips are chosen by overlapping the window, not by containing the press.
    assert "s < at + AFTER and e > at - BEFORE" in fn


def test_finding_the_clips_does_not_read_every_sidecar():
    # One press would otherwise mean a file read per clip in the archive.
    src = read_file("web/marks.py")
    fn = src[src.index("def _candidate_clips("):]
    fn = fn[:fn.index("\ndef ")]
    assert "lo <= m <= hi" in fn, "the indexed column is not used to narrow first"
    assert fn.index("lo <= m <= hi") < fn.index("device_times")


def test_the_excerpt_is_joined_not_fragments():
    # A reminder that stops halfway through a sentence is not a reminder.
    src = read_file("web/marks.py")
    assert "threads.for_conversation" in src


def test_the_route_offers_both_the_log_and_the_reading():
    src = read_file("web/server.py")
    assert '@app.get("/api/marks")' in src, "no readable form"
    assert '@app.get("/api/moments")' in src, "the raw log is gone"
    fn = src[src.index('@app.get("/api/marks")'):]
    fn = fn[:fn.index("\n@app.")]
    assert "run_in_executor" in fn, "this reads transcripts off the event loop"
    assert "kind" in fn
