"""Buying the diarization, keeping the identity.

Diarization is what a GPU-less machine cannot keep up with -- 0.47x realtime
on a CPU, slower than the microphone. Deepgram returns who spoke as well as
what was said. What it cannot return is a voiceprint, and without one nobody
in the archive is ever named, so those are still made here.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import asr_deepgram
import embedder

WORDS = {"results": {"channels": [{"alternatives": [{"words": [
    {"word": "hello", "punctuated_word": "Hello", "start": 0.0, "end": 0.4,
     "confidence": 0.99, "speaker": 0},
    {"word": "there", "punctuated_word": "there.", "start": 0.4, "end": 0.9,
     "confidence": 0.98, "speaker": 0},
    {"word": "hi", "punctuated_word": "Hi.", "start": 1.2, "end": 1.5,
     "confidence": 0.97, "speaker": 1},
    {"word": "again", "punctuated_word": "Again?", "start": 1.6, "end": 2.0,
     "confidence": 0.9, "speaker": 0},
]}]}]}}


def test_a_segment_never_holds_two_speakers():
    # A segment with two people in it cannot be labelled with either.
    segs = asr_deepgram._to_whisperx(WORDS)
    assert [s["speaker"] for s in segs] == ["SPEAKER_00", "SPEAKER_01",
                                            "SPEAKER_00"]
    assert segs[0]["text"] == "Hello there."
    assert segs[1]["text"] == "Hi."


def test_their_numbering_is_written_the_way_the_diarizer_writes_it():
    # A label has to mean the same thing whichever path produced it.
    segs = asr_deepgram._to_whisperx(WORDS)
    assert all(s["speaker"].startswith("SPEAKER_") for s in segs)


def test_word_confidence_is_carried_not_invented():
    segs = asr_deepgram._to_whisperx(WORDS)
    assert segs[0]["words"][0]["score"] == 0.99


def test_a_speaker_label_does_not_survive_on_the_word():
    # It lives on the segment; two places to read it is two places to drift.
    segs = asr_deepgram._to_whisperx(WORDS)
    assert "speaker" not in segs[0]["words"][0]


def test_rubbish_from_the_service_is_no_transcript_not_a_crash():
    assert asr_deepgram._to_whisperx({}) == []
    assert asr_deepgram._to_whisperx({"results": {}}) == []


def test_no_key_is_reported_not_raised_as_a_crash():
    import secrets_store, tempfile
    secrets_store.PATH = os.path.join(tempfile.mkdtemp(), "secrets.json")
    saved = os.environ.pop("DEEPGRAM_API_KEY", None)
    try:
        assert asr_deepgram.available() is False
        try:
            asr_deepgram.transcribe("/dev/null")
        except asr_deepgram.Unavailable as e:
            assert "key" in str(e).lower()
        else:
            raise AssertionError("no key should be an Unavailable")
    finally:
        if saved is not None:
            os.environ["DEEPGRAM_API_KEY"] = saved


# ------------------------------------------------ voiceprints, made here
def test_a_speakers_stretches_are_pooled_into_one_voiceprint():
    """The matcher expects one vector per person, pooled over everything they
    said -- which is what the diarizer's own embeddings are."""
    seen = []
    def embed(chunk):
        seen.append(len(chunk))
        return np.ones(8)

    audio = np.zeros(16000 * 4, dtype=np.float32)
    spans = {"SPEAKER_00": [(0.0, 1.0), (2.0, 3.0)]}
    out = embedder.voiceprints(embed, audio, 16000, spans)
    assert list(out) == ["SPEAKER_00"]
    assert seen == [32000], "the two stretches were not embedded as one"


def test_a_fragment_is_not_worth_a_voiceprint():
    # A vector built from a fraction of a second matches everybody equally,
    # which is worse than no answer.
    out = embedder.voiceprints(lambda c: np.ones(8),
                               np.zeros(16000, dtype=np.float32), 16000,
                               {"SPEAKER_00": [(0.0, 0.2)]})
    assert out == {}


def test_a_non_finite_vector_is_dropped():
    # One makes the transcript unencodable, so the clip looks untranscribed
    # forever and re-transcribing reproduces it.
    out = embedder.voiceprints(lambda c: np.array([np.nan] * 8),
                               np.zeros(16000 * 2, dtype=np.float32), 16000,
                               {"SPEAKER_00": [(0.0, 2.0)]})
    assert out == {}


def test_one_unusable_speaker_is_not_a_failed_clip():
    def embed(chunk):
        raise RuntimeError("no")
    out = embedder.voiceprints(embed, np.zeros(16000 * 2, dtype=np.float32),
                               16000, {"SPEAKER_00": [(0.0, 2.0)]})
    assert out == {}


def test_spans_come_from_the_segments_that_carry_a_speaker():
    spans = embedder.spans_from_segments([
        {"start": 0, "end": 1, "speaker": "SPEAKER_00"},
        {"start": 1, "end": 2, "speaker": None},
        {"start": 2, "end": 3, "speaker": "SPEAKER_00"}])
    assert spans == {"SPEAKER_00": [(0.0, 1.0), (2.0, 3.0)]}


def test_the_cloud_path_still_makes_voiceprints_here():
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "pipeline.py")).read()
    assert "cloud_diarized" in src
    assert "embedder.voiceprints" in src
    assert "self._embed_audio" in src, "a second embedder would answer a "\
                                       "different question from the archive's"
