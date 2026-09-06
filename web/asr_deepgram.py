"""Transcribe and diarize in one call, off this machine.

OpenAI fixes the words. This fixes the other half: Deepgram returns who spoke
as well as what was said, in the same request, which is the stage that
actually makes a GPU-less machine fall behind -- diarization runs at 0.47x
realtime on a CPU, slower than the microphone.

What it cannot return is a voiceprint, and without one nobody can be named.
So the diarizing is bought and the identity stays here: web/embedder.py makes
the vectors locally, at 19x realtime on a CPU, because the expensive part of
pyannote is the segmentation and the clustering rather than the embedding.

`nova-3` with `diarize=true` and `punctuate=true`. Words come back with their
own timings and a speaker number each, which is the shape everything
downstream already wants -- speakers are assigned to words here.
"""

import json
import os
import urllib.error
import urllib.request

import secrets_store

API = ("https://api.deepgram.com/v1/listen"
       "?model=nova-3&diarize=true&punctuate=true&smart_format=true")
MAX_BYTES = 100 * 1024 * 1024


class Unavailable(RuntimeError):
    """No key, no network, or the service said no. Never a reason to lose
    audio: the caller falls back to the local models."""


def available():
    return bool(secrets_store.get("DEEPGRAM_API_KEY"))


def transcribe(path, timeout=120):
    """Returns whisperx's post-alignment shape, with speakers already set.

        {"segments": [{"start", "end", "text", "speaker",
                       "words": [{"word", "start", "end", "score"}]}]}
    """
    key = secrets_store.get("DEEPGRAM_API_KEY")
    if not key:
        raise Unavailable("no Deepgram key — Settings → API keys")

    data = open(path, "rb").read()
    if len(data) > MAX_BYTES:
        raise Unavailable(f"{len(data)/1e6:.1f} MB is too large to send")

    req = urllib.request.Request(API, data=data, method="POST", headers={
        "Authorization": f"Token {key}", "Content-Type": "audio/wav"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("err_msg", "")
        except Exception:
            pass
        # The key is never in the message: a refusal is reported as a
        # refusal, not by quoting what was sent.
        raise Unavailable(f"Deepgram said {e.code}"
                          + (f": {detail[:120]}" if detail else ""))
    except Exception as e:
        raise Unavailable(f"{type(e).__name__}: {str(e)[:120]}")

    return {"segments": _to_whisperx(payload)}


def _to_whisperx(payload):
    """Their words into our segments, split wherever the speaker changes.

    Deepgram returns one flat list of words, each carrying a speaker number.
    A segment here is a run of words by one person: splitting on the speaker
    change is the only division that matters downstream, because a segment
    with two speakers in it cannot be labelled with either.
    """
    try:
        alt = payload["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        return []

    words = []
    for w in alt.get("words") or []:
        if w.get("start") is None or w.get("end") is None:
            continue
        who = w.get("speaker")
        words.append({
            "word": w.get("punctuated_word") or w.get("word") or "",
            "start": float(w["start"]), "end": float(w["end"]),
            # Theirs is a real per-word confidence, unlike OpenAI's.
            "score": float(w.get("confidence", 1.0)),
            # Their numbering, made to look like the diarizer's, so a label
            # means the same thing whichever path produced it.
            "speaker": None if who is None else f"SPEAKER_{int(who):02d}",
        })

    out = []
    for w in words:
        last = out[-1] if out else None
        if last is not None and last["speaker"] == w["speaker"]:
            last["words"].append(w)
            last["end"] = w["end"]
            last["text"] += " " + w["word"]
        else:
            out.append({"start": w["start"], "end": w["end"],
                        "speaker": w["speaker"], "text": w["word"],
                        "words": [w]})
    for seg in out:
        seg["text"] = seg["text"].strip()
        for w in seg["words"]:
            w.pop("speaker", None)     # it lives on the segment now
    return out
