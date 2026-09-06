"""Voiceprints, without the diarizer that usually comes with them.

pyannote hands back a diarization and its embeddings together, and that is
fine until the diarizing happens somewhere else. A cloud service can say who
spoke when, quickly, on a machine with no GPU -- but it cannot hand back a
voiceprint, and without one nobody in the archive can ever be named. Speaker
identity is the whole point of this project, so a cloud path that gave up
names would not be worth having.

It turns out not to be a trade. Measured on this machine, one 30-second clip,
three speakers:

    diarize + embed together (pyannote)   0.47x realtime on CPU
    embed alone (this)                   19.1x realtime on CPU
                                        143.7x realtime on GPU

The expensive part is the segmentation and the clustering, not the embedding.
So the words and the turn-taking can be bought, and the identity stays here.
"""

import numpy as np

MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"
# Below this a voiceprint is not worth making. pyannote returns a non-finite
# embedding when a cluster has too little audio to take a standard deviation
# over, and a vector built from a fragment of a second matches everybody
# equally, which is worse than no answer.
MIN_SECONDS = 0.8


def voiceprints(embed, audio, sr, spans):
    """One vector per speaker, from the audio they actually spoke.

    `spans` maps a speaker label to the (start, end) pairs where that speaker
    was talking. Each speaker's stretches are concatenated and embedded once,
    so the vector is pooled over everything they said in the clip -- which is
    what the diarizer's own embeddings are, and what the matcher expects.

    `embed` is passed in rather than loaded here, and is the same model the
    local diarizer already holds. A second embedder would answer a slightly
    different question from the vectors already in the archive, and every
    stored voiceprint was made by that one.
    """
    audio = np.asarray(audio, dtype=np.float32).ravel()
    out = {}
    for speaker, pairs in (spans or {}).items():
        pieces = []
        for start, end in pairs:
            a, b = int(max(0.0, start) * sr), int(max(0.0, end) * sr)
            if b > a:
                pieces.append(audio[a:min(b, len(audio))])
        if not pieces:
            continue
        chunk = np.concatenate(pieces)
        if len(chunk) < MIN_SECONDS * sr:
            continue
        try:
            vec = embed(chunk)
        except Exception:
            continue          # one unusable speaker is not a failed clip
        if vec is None:
            continue
        arr = np.asarray(vec, dtype=np.float64).ravel()
        # Same guard the diarization path has: a non-finite vector makes the
        # transcript unencodable, so the clip looks untranscribed forever.
        if arr.size and np.all(np.isfinite(arr)):
            out[speaker] = arr
    return out


def spans_from_segments(segments):
    """Where each speaker talked, from segments that carry a speaker label."""
    spans = {}
    for s in segments or []:
        who = s.get("speaker")
        if who is None:
            continue
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            spans.setdefault(who, []).append((start, end))
    return spans
