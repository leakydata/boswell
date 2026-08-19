#!/usr/bin/env python3
"""
Background transcription worker for the web UI.

Runs in a thread so the event loop keeps servicing Bluetooth while a clip is
transcribed. The ASR and diarization models are loaded once on the first job
and kept resident -- reloading them per clip would cost ~15 s every time.
"""

import json
import math
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


SAMPLES_DB = os.path.join(DATA, "speaker_samples.npz")

# A voiceprint from a few seconds of speech is unreliable, and averaging one in
# permanently drags the reference away from how the person actually sounds.
MIN_ENROLL_SECONDS = 5.0
# Measured on this hardware: the same speaker across recordings scores
# 0.65-0.87, two different speakers 0.38-0.48. 0.55 sits in the gap -- low
# enough to allow genuine variation in how someone sounds, high enough to
# catch a line that belongs to somebody else. An earlier 0.40 sat inside the
# different-speaker range and let a wrong voice enrol without complaint.
OUTLIER_MIN = 0.55


def load_speakers():
    """Reference vectors used for matching, derived from the stored samples."""
    if not os.path.exists(SPEAKER_DB):
        return {}
    d = np.load(SPEAKER_DB)
    return {k: unit(d[k]) for k in d.files}


def _load_samples():
    if not os.path.exists(SAMPLES_DB):
        return {}
    d = np.load(SAMPLES_DB)
    return {k: d[k] for k in d.files}


def _save_samples(samples):
    os.makedirs(DATA, exist_ok=True)
    np.savez(SAMPLES_DB, **samples)


def _load_meta():
    return json.load(open(SPEAKER_META)) if os.path.exists(SPEAKER_META) else {}


def _save_meta(meta):
    os.makedirs(DATA, exist_ok=True)
    json.dump(meta, open(SPEAKER_META, "w"), indent=2)


def _migrate(meta, samples):
    """Adopt any pre-existing centroid as a single weighted sample.

    Earlier versions stored only the average, so the individual recordings
    behind it are gone. Carrying the average forward with its original weight
    preserves every match that already worked, and new labels accumulate
    alongside it as removable samples.
    """
    if not os.path.exists(SPEAKER_DB):
        return False
    d = np.load(SPEAKER_DB)
    changed = False
    for name in d.files:
        entry = meta.setdefault(name, {})
        if entry.get("samples"):
            continue
        weight = int(entry.get("count", 1)) or 1
        sid = "legacy"
        samples[f"{name}||{sid}"] = unit(d[name])
        entry["samples"] = [{"id": sid, "weight": weight, "legacy": True,
                             "clip": None, "seconds": None, "score": None}]
        changed = True
    return changed


def recompute_centroid(name, meta, samples):
    """Weighted mean of a person's samples, renormalised for cosine matching."""
    entries = meta.get(name, {}).get("samples", [])
    acc = None
    for e in entries:
        v = samples.get(f"{name}||{e['id']}")
        if v is None:
            continue
        w = float(e.get("weight", 1))
        acc = unit(v) * w if acc is None else acc + unit(v) * w
    vecs = {}
    if os.path.exists(SPEAKER_DB):
        d = np.load(SPEAKER_DB)
        vecs = {k: d[k] for k in d.files}
    if acc is None:
        vecs.pop(name, None)
    else:
        vecs[name] = unit(acc)
    np.savez(SPEAKER_DB, **vecs)


def list_speakers():
    meta = _load_meta()
    samples = _load_samples()
    if _migrate(meta, samples):
        _save_samples(samples)
        _save_meta(meta)
    out = []
    for name, entry in sorted(meta.items()):
        out.append({
            "name": name,
            "samples": entry.get("samples", []),
            "count": sum(int(e.get("weight", 1)) for e in entry.get("samples", [])),
        })
    return out


def save_speaker(name, vec, clip=None, speaker=None, seconds=None, force=False):
    """Add one sample to a person, with quality checks.

    Returns a dict describing what happened. Rejections are advisory: passing
    force=True enrols anyway, because only the person listening can settle a
    genuinely unusual-sounding recording.
    """
    meta = _load_meta()
    samples = _load_samples()
    _migrate(meta, samples)

    v = unit(vec)
    entry = meta.setdefault(name, {})
    entry.setdefault("samples", [])

    if not force:
        if seconds is not None and seconds < MIN_ENROLL_SECONDS:
            return {"ok": False, "reason": "too_short", "seconds": round(seconds, 1),
                    "minimum": MIN_ENROLL_SECONDS,
                    "detail": f"only {seconds:.1f}s of this voice in the clip; "
                              f"{MIN_ENROLL_SECONDS:.0f}s or more makes a reliable voiceprint"}
        existing = load_speakers().get(name)
        if existing is not None and entry["samples"]:
            sim = float(v @ unit(existing))
            if sim < OUTLIER_MIN:
                return {"ok": False, "reason": "outlier", "similarity": round(sim, 3),
                        "minimum": OUTLIER_MIN,
                        "detail": f"this sounds unlike the {name} already enrolled "
                                  f"({sim:.2f}); it may be a misattributed line"}

    sid = f"s{int(time.time() * 1000) % 100000000}"
    samples[f"{name}||{sid}"] = v
    score = None
    existing = load_speakers().get(name)
    if existing is not None and entry["samples"]:
        score = round(float(v @ unit(existing)), 3)
    entry["samples"].append({"id": sid, "weight": 1, "clip": clip,
                             "speaker": speaker,
                             "seconds": round(seconds, 1) if seconds else None,
                             "score": score})
    _save_samples(samples)
    _save_meta(meta)
    recompute_centroid(name, meta, samples)
    return {"ok": True, "name": name,
            "count": sum(int(e.get("weight", 1)) for e in entry["samples"]),
            "score": score}


def delete_sample(name, sample_id):
    meta = _load_meta()
    samples = _load_samples()
    entry = meta.get(name)
    if not entry:
        return False
    before = len(entry.get("samples", []))
    entry["samples"] = [e for e in entry.get("samples", []) if e["id"] != sample_id]
    samples.pop(f"{name}||{sample_id}", None)
    if not entry["samples"]:
        meta.pop(name, None)
    _save_samples(samples)
    _save_meta(meta)
    recompute_centroid(name, meta, samples)
    return len(entry.get("samples", [])) != before if name in meta else True


def delete_speaker(name):
    meta = _load_meta()
    samples = _load_samples()
    if name not in meta:
        return False
    for e in meta[name].get("samples", []):
        samples.pop(f"{name}||{e['id']}", None)
    meta.pop(name, None)
    _save_samples(samples)
    _save_meta(meta)
    recompute_centroid(name, meta, samples)
    return True


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


VOCAB_PATH = os.path.join(DATA, "vocabulary.json")
# Below this, a fuzzy match is more likely to corrupt a correct word than to
# fix a wrong one.
FUZZY_CUTOFF = 0.82
MIN_FUZZY_LEN = 5


def load_vocabulary():
    """Domain words the model would otherwise mangle: drug names, project
    names, jargon, and the people you have enrolled."""
    terms = []
    if os.path.exists(VOCAB_PATH):
        try:
            terms = list(json.load(open(VOCAB_PATH)).get("terms", []))
        except Exception:
            terms = []
    # Enrolled names are exactly the words an ASR model gets wrong, so they
    # are always boosted without needing to be typed in twice.
    try:
        for p in list_speakers():
            if p["name"] not in terms:
                terms.append(p["name"])
    except Exception:
        pass
    return [t.strip() for t in terms if t and t.strip()]


def save_vocabulary(terms):
    os.makedirs(DATA, exist_ok=True)
    clean, seen = [], set()
    for t in terms:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean.append(t)
    json.dump({"terms": clean}, open(VOCAB_PATH, "w"), indent=2)
    return clean


def vocabulary_signature(terms):
    return "|".join(sorted(t.lower() for t in terms))


def apply_vocabulary(text, terms):
    """Fix near-misses the decoder bias did not catch.

    Boosting makes a term likelier, it does not guarantee it, so "Metformin"
    can still land as "metformen" or, worse, be split into "met foreman".
    Two passes handle those separately: a phrase pass that re-joins runs of
    words, then a word pass. Only reasonably long terms are fuzzy-matched --
    a wrong correction on a short word does more damage than a missed one.
    """
    import difflib
    import re as _re

    if not terms:
        return text

    exact = {t.lower(): t for t in terms}
    letters = lambda w: _re.sub(r"[^a-z0-9]", "", w.lower())
    # Terms worth trying to reassemble from several words.
    joined = {letters(t): t for t in terms if len(letters(t)) >= MIN_FUZZY_LEN}

    token = _re.compile(r"[A-Za-z][A-Za-z0-9'-]*")

    # Pass 1: windows of 3 then 2 words, in case the term was split apart.
    for width in (3, 2):
        while True:
            toks = list(token.finditer(text))
            replaced = False
            for i in range(len(toks) - width + 1):
                span = toks[i:i + width]
                # Only merge words that are adjacent with plain spaces.
                between = text[span[0].end():span[-1].start()]
                if _re.search(r"[^ ]", between):
                    continue
                cand = letters("".join(m.group(0) for m in span))
                if len(cand) < MIN_FUZZY_LEN:
                    continue
                hit = difflib.get_close_matches(cand, joined.keys(), n=1, cutoff=0.87)
                if hit:
                    text = text[:span[0].start()] + joined[hit[0]] + text[span[-1].end():]
                    replaced = True
                    break
            if not replaced:
                break

    # Pass 2: single words.
    def fix(m):
        w = m.group(0)
        low = w.lower()
        if low in exact:
            return exact[low]
        key = letters(w)
        if key in joined:
            return joined[key]
        if len(w) < MIN_FUZZY_LEN:
            return w
        hit = difflib.get_close_matches(key, joined.keys(), n=1, cutoff=FUZZY_CUTOFF)
        return joined[hit[0]] if hit else w

    return token.sub(fix, text)


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
        self._vocab_sig = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, clip):
        self.q.put(clip)

    def _load(self):
        terms = load_vocabulary()
        sig = vocabulary_signature(terms)
        # Boosting is baked in when the model is built, so a changed word list
        # means rebuilding it. Cheap relative to how often the list changes.
        if self._asr is not None and sig == self._vocab_sig:
            return
        reloading = self._asr is not None
        self.notify("log", text=("reloading ASR with the updated word list"
                                 if reloading else
                                 "loading transcription models (first run, ~20s)"))
        import whisperx
        # Decode-time boosting is deliberately NOT used. Measured on a real
        # 45-second recording: plain decoding produced two segments covering
        # the whole clip, while hotwords (with or without an initial_prompt)
        # produced one and silently dropped sixteen seconds of speech.
        # Conditioning the decoder on a bare word list makes it treat some
        # chunks as containing nothing.
        #
        # Custom words are applied afterwards instead, in apply_vocabulary().
        # That fixes the same mistakes -- including terms split across words --
        # and cannot cause audio to go missing.
        self._vocab_sig = sig
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
            # pyannote returns a NaN embedding when a speaker cluster has too
            # little audio to compute a standard deviation over. Storing one
            # produces a transcript that cannot be encoded as valid JSON, so
            # the clip becomes unreadable and looks untranscribed forever --
            # and re-transcribing reproduces it. A NaN voiceprint could never
            # match anything anyway, so drop it and keep the transcript.
            clean = {}
            for k, v in (emb or {}).items():
                arr = np.asarray(v, dtype=np.float64).ravel()
                if arr.size and np.all(np.isfinite(arr)):
                    clean[k] = arr
            embeddings = {k: v.tolist() for k, v in clean.items()}
            names = identify(clean)

        terms = load_vocabulary()
        segs = [{"start": round(float(s["start"]), 2),
                 "end": round(float(s["end"]), 2),
                 "speaker": s.get("speaker"),
                 "text": apply_vocabulary(s["text"].strip(), terms)}
                for s in res["segments"] if s.get("text", "").strip()]

        # Drop any segment whose timing did not survive alignment, for the
        # same reason: one non-finite value makes the whole file unservable.
        segs = [x for x in segs
                if all(isinstance(x[k], (int, float)) and math.isfinite(x[k])
                       for k in ("start", "end"))]

        os.makedirs(TRANSCRIPTS, exist_ok=True)
        json.dump({"clip": clip, "created": time.time(), "segments": segs,
                   "speakers": names, "embeddings": embeddings},
                  open(transcript_path(clip), "w"), indent=2, allow_nan=False)
