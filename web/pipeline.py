#!/usr/bin/env python3
"""
Background transcription worker for the web UI.

Runs in a thread so the event loop keeps servicing Bluetooth while a clip is
transcribed. The ASR and diarization models are loaded once on the first job
and kept resident -- reloading them per clip would cost ~15 s every time.
"""

import json
import os
import queue
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
TRANSCRIPTS = os.path.join(DATA, "transcripts")
SPEAKER_DB = os.path.join(DATA, "speakers.npz")
SPEAKER_META = os.path.join(DATA, "speakers.json")
MATCH_THRESHOLD = 0.60


def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def load_speakers():
    if not os.path.exists(SPEAKER_DB):
        return {}
    d = np.load(SPEAKER_DB)
    return {k: unit(d[k]) for k in d.files}


def save_speaker(name, vec):
    """Append to a running centroid. No training -- enrolment is a mean."""
    vecs = {}
    if os.path.exists(SPEAKER_DB):
        d = np.load(SPEAKER_DB)
        vecs = {k: d[k] for k in d.files}
    meta = json.load(open(SPEAKER_META)) if os.path.exists(SPEAKER_META) else {}

    v = unit(vec)
    if name in vecs:
        n = meta.get(name, {}).get("count", 1)
        vecs[name] = unit(unit(vecs[name]) * n + v)
        meta.setdefault(name, {})["count"] = n + 1
    else:
        vecs[name] = v
        meta[name] = {"count": 1}

    os.makedirs(DATA, exist_ok=True)
    np.savez(SPEAKER_DB, **vecs)
    json.dump(meta, open(SPEAKER_META, "w"), indent=2)
    return meta[name]["count"]


def identify(embeddings):
    db = load_speakers()
    out = {}
    for spk, vec in (embeddings or {}).items():
        v = unit(vec)
        best, name = -1.0, None
        for n, ref in db.items():
            s = float(v @ ref)
            if s > best:
                best, name = s, n
        out[spk] = {"name": name if best >= MATCH_THRESHOLD else None,
                    "score": round(best, 3) if name else 0.0}
    return out


def transcript_path(clip):
    return os.path.join(TRANSCRIPTS, os.path.splitext(clip)[0] + ".json")


class Worker:
    def __init__(self, notify=None):
        self.q: queue.Queue = queue.Queue()
        self.notify = notify or (lambda *a, **k: None)
        self.busy = None
        self._asr = None
        self._align = None
        self._diar = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, clip):
        self.q.put(clip)

    def _load(self):
        if self._asr is not None:
            return
        self.notify("log", text="loading transcription models (first run, ~20s)")
        import whisperx
        self._asr = whisperx.load_model("large-v3", "cuda",
                                        compute_type="float16", language="en")
        self._align = whisperx.load_align_model(language_code="en", device="cuda")
        token = os.environ.get("HF_TOKEN")
        if token:
            try:
                self._diar = whisperx.diarize.DiarizationPipeline(
                    model_name="pyannote/speaker-diarization-3.1",
                    token=token, device="cuda")
            except Exception as e:
                self.notify("log", text=f"diarization unavailable: {str(e)[:80]}")
        self.notify("log", text="models ready")

    def _run(self):
        while True:
            clip = self.q.get()
            try:
                self.busy = clip
                self.notify("job", clip=clip, status="running")
                self._process(clip)
                self.notify("job", clip=clip, status="done")
            except Exception as e:
                self.notify("job", clip=clip, status="error", error=str(e)[:200])
                self.notify("log", text=f"transcription failed: {str(e)[:120]}")
            finally:
                self.busy = None

    def _process(self, clip):
        import whisperx
        self._load()
        path = os.path.join(DATA, clip)
        audio = whisperx.load_audio(path)

        res = self._asr.transcribe(audio, batch_size=16)
        model_a, meta = self._align
        res = whisperx.align(res["segments"], model_a, meta, audio, "cuda")

        names, embeddings = {}, {}
        if self._diar is not None:
            df, emb = self._diar(audio, return_embeddings=True)
            res = whisperx.assign_word_speakers(df, res)
            embeddings = {k: np.asarray(v).ravel().tolist()
                          for k, v in (emb or {}).items()}
            names = identify(emb or {})

        segs = [{"start": round(float(s["start"]), 2),
                 "end": round(float(s["end"]), 2),
                 "speaker": s.get("speaker"),
                 "text": s["text"].strip()}
                for s in res["segments"] if s.get("text", "").strip()]

        os.makedirs(TRANSCRIPTS, exist_ok=True)
        json.dump({"clip": clip, "created": time.time(), "segments": segs,
                   "speakers": names, "embeddings": embeddings},
                  open(transcript_path(clip), "w"), indent=2)
