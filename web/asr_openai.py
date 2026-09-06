"""Transcribe a clip with OpenAI instead of the local model.

Why this exists: without an NVIDIA GPU, local transcription runs at about
0.79x realtime for large-v3 -- slower than the microphone produces audio, so
the backlog never closes. Sending the audio out solves that half of the
problem for anybody who would rather pay for a key than buy a card.

**It solves that half only.** Diarization still runs locally, and on a CPU
that is the slower stage of the two: 0.47x realtime, 63.7 s to work out who
spoke in 30 s of audio. So this makes the words keep up; it does not make the
speakers keep up. A machine with no GPU and no Hugging Face token gets
transcripts with nobody named, quickly. A machine with no GPU and diarization
switched on is still behind, and this cannot fix that.

The model is `whisper-1` on purpose. It is the one that returns
`verbose_json` with word timestamps, and word timings are not a nicety here:
speakers are assigned to words, so a transcript without them cannot carry
speaker labels at all. The newer gpt-4o transcription models return text and
segments but no words, which would quietly cost every name in the archive.

Word timestamps also mean the local alignment pass can be skipped entirely,
which is the second-slowest stage on a CPU.
"""

import json
import os
import urllib.error
import urllib.request

import secrets_store

API = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "whisper-1"
# Their documented ceiling. A 30-second clip is nowhere near it; a
# consolidation pass over a long conversation could be, so it is checked
# rather than discovered as a 413 halfway through an evening.
MAX_BYTES = 25 * 1024 * 1024


class Unavailable(RuntimeError):
    """No key, no network, or the service said no. Never a reason to lose
    audio: the caller falls back to the local model."""


def available():
    return bool(secrets_store.get("OPENAI_API_KEY"))


def _multipart(fields, filename, data):
    """One multipart body. Written out rather than pulled in, because the
    only thing this needs from a library is a boundary string."""
    boundary = "----boswell" + os.urandom(16).hex()
    out = bytearray()
    for k, v in fields:
        out += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{v}\r\n").encode()
    out += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n").encode()
    out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def transcribe(path, timeout=120):
    """Returns whisperx's post-alignment shape, so nothing downstream changes.

        {"segments": [{"start", "end", "text",
                       "words": [{"word", "start", "end", "score"}]}]}
    """
    key = secrets_store.get("OPENAI_API_KEY")
    if not key:
        raise Unavailable("no OpenAI key — Settings → API keys")

    data = open(path, "rb").read()
    if len(data) > MAX_BYTES:
        raise Unavailable(f"{len(data)/1e6:.1f} MB is over the 25 MB limit")

    fields = [("model", MODEL), ("response_format", "verbose_json"),
              ("timestamp_granularities[]", "segment"),
              ("timestamp_granularities[]", "word")]
    body, content_type = _multipart(fields, os.path.basename(path), data)
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            pass
        # The key itself is never in the message. A 401 here is reported as
        # "the key was refused", not by quoting what was sent.
        raise Unavailable(f"OpenAI said {e.code}"
                          + (f": {detail[:120]}" if detail else ""))
    except Exception as e:
        raise Unavailable(f"{type(e).__name__}: {str(e)[:120]}")

    return {"segments": _to_whisperx(payload), "language": payload.get("language")}


def _to_whisperx(payload):
    """Their shape into ours: words attached to the segment they fall in.

    They are returned as two flat lists -- segments, and words with their own
    timings -- and everything downstream expects words nested under the
    segment that contains them.
    """
    words = []
    for w in payload.get("words") or []:
        if w.get("start") is None or w.get("end") is None:
            continue
        words.append({"word": w.get("word", ""),
                      "start": float(w["start"]), "end": float(w["end"]),
                      # whisperx carries a per-word confidence; theirs does
                      # not. 1.0 rather than 0.0, because the value is used
                      # as a weight and a zero would silently discard the
                      # word wherever it is read as one.
                      "score": 1.0})
    out, i = [], 0
    for seg in payload.get("segments") or []:
        start, end = float(seg.get("start", 0.0)), float(seg.get("end", 0.0))
        mine = []
        while i < len(words) and words[i]["start"] < end:
            mine.append(words[i])
            i += 1
        out.append({"start": start, "end": end,
                    "text": (seg.get("text") or "").strip(), "words": mine})
    if not out and payload.get("text"):
        # No segments but text came back. Better one segment covering the
        # clip than a transcript that reads as silence.
        out = [{"start": 0.0, "end": float(payload.get("duration") or 0.0),
                "text": payload["text"].strip(), "words": words}]
    return out
