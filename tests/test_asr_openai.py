"""Transcribing somewhere other than this machine.

Without an NVIDIA card the local model runs at 0.79x realtime -- slower than
the microphone -- so the audio can be sent out instead. These cover the
translation between their shape and ours, and the failures that must never
cost a recording.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import asr_openai


def test_words_land_under_the_segment_that_contains_them():
    """They return two flat lists; everything downstream expects words nested
    under their segment, because speakers are assigned to words."""
    out = asr_openai._to_whisperx({
        "segments": [{"start": 0.0, "end": 2.0, "text": " Hello there. "},
                     {"start": 2.0, "end": 4.0, "text": "Goodbye."}],
        "words": [{"word": "Hello", "start": 0.1, "end": 0.5},
                  {"word": "there", "start": 0.6, "end": 1.2},
                  {"word": "Goodbye", "start": 2.1, "end": 2.9}],
    })
    assert [s["text"] for s in out] == ["Hello there.", "Goodbye."]
    assert [w["word"] for w in out[0]["words"]] == ["Hello", "there"]
    assert [w["word"] for w in out[1]["words"]] == ["Goodbye"]


def test_every_word_carries_a_usable_score():
    # whisperx carries a per-word confidence and theirs does not. A zero
    # would silently discard the word wherever the value is read as a weight.
    out = asr_openai._to_whisperx({
        "segments": [{"start": 0, "end": 1, "text": "hi"}],
        "words": [{"word": "hi", "start": 0.0, "end": 0.4}]})
    assert out[0]["words"][0]["score"] == 1.0


def test_a_word_with_no_timing_is_dropped_not_kept_at_zero():
    # A word at 0.0 would be assigned to whoever spoke first, forever.
    out = asr_openai._to_whisperx({
        "segments": [{"start": 0, "end": 1, "text": "hi"}],
        "words": [{"word": "hi", "start": None, "end": None}]})
    assert out[0]["words"] == []


def test_text_with_no_segments_is_still_a_transcript():
    # Better one segment covering the clip than a transcript that reads as
    # silence.
    out = asr_openai._to_whisperx({"text": "something was said",
                                   "duration": 30.0, "segments": [],
                                   "words": []})
    assert len(out) == 1 and out[0]["text"] == "something was said"
    assert out[0]["end"] == 30.0


def test_no_key_is_reported_not_raised_as_a_crash():
    import secrets_store, tempfile
    secrets_store.PATH = os.path.join(tempfile.mkdtemp(), "secrets.json")
    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        assert asr_openai.available() is False
        try:
            asr_openai.transcribe("/dev/null")
        except asr_openai.Unavailable as e:
            assert "key" in str(e).lower()
        else:
            raise AssertionError("no key should be an Unavailable")
    finally:
        if saved is not None:
            os.environ["OPENAI_API_KEY"] = saved


def test_a_cloud_failure_falls_back_instead_of_losing_the_clip():
    # The clip is on disk either way. Refusing to transcribe because an API
    # was down would turn a paid convenience into a way to lose a
    # conversation.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "pipeline.py")).read()
    body = src[src.index("def _words("):src.index("def transcriber(")]
    assert "except asr_openai.Unavailable" in body
    assert "self._asr.transcribe" in body, "no local fallback in the same path"


def test_the_local_model_is_not_loaded_to_sit_idle():
    # The point of the cloud path is the machine that cannot hold this model
    # at all.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "pipeline.py")).read()
    assert 'if self.transcriber() == "openai" and not getattr' in src
    assert "_load_helpers" in src, "diarization must still load"


def test_the_model_is_the_one_that_returns_word_timings():
    # gpt-4o transcription returns text and segments but no words, which
    # would quietly cost every name in the archive.
    assert asr_openai.MODEL == "whisper-1"
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "asr_openai.py")).read()
    assert 'timestamp_granularities[]", "word"' in src
