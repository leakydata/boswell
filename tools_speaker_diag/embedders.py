#!/usr/bin/env python3
"""Would a different embedder recognise a voice across days?

The measurement in drift.py says the current one does not. Same person an hour
later scores 0.82; a day later, 0.52 -- and different people under matched
conditions reach 0.57 at the 99th percentile, so those two overlap and no
threshold separates them. Everything the store does to work around that
(a reference per condition, a labelling queue, naming somebody again tomorrow)
exists because of that number.

So it is worth an hour to find out whether the number is a property of speech
or a property of this particular model. If another embedder holds a voice
together across days on the SAME audio, the workaround is unnecessary and most
of the manual labelling disappears.

Same audio, same spans, two models, one comparison. Nothing is re-recorded and
nothing in the store is touched; this only reads.

    uv run tools_speaker_diag/embedders.py [--person Nathan] [--spans 50]
"""
import argparse
import collections
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "web"))

import pipeline                       # noqa: E402
import speaker_store as store         # noqa: E402

# The one in use, and the usual alternative. Both are wrapped by pyannote to a
# common interface, so the only thing that differs between the two runs is the
# weights.
MODELS = [
    ("wespeaker (current)", "pyannote/wespeaker-voxceleb-resnet34-LM"),
    ("ecapa-tdnn", "speechbrain/spkrec-ecapa-voxceleb"),
]

BINS = [("< 1 hour", 3600), ("1-6 hours", 6 * 3600), ("6-24 hours", 86400),
        ("1-3 days", 3 * 86400), ("> 3 days", float("inf"))]

MIN_SPAN_SECONDS = 4.0
MAX_SPAN_SECONDS = 20.0


def clip_time(name):
    m = re.search(r"(\d{9,})", name or "")
    return int(m.group(1)) if m else None


def gather_spans(person, limit):
    """Audio for one person: their turns in each clip they were labelled in.

    Sampled evenly across the whole span rather than taking the first N, so a
    single busy afternoon cannot stand in for eleven days.
    """
    c = store._conn()
    try:
        pid = store.person_id_for(person, c, create=False)
        if pid is None:
            sys.exit(f"nobody called {person!r} in the store")
        rows = c.execute("SELECT clip, speaker FROM voiceprints "
                         "WHERE person_id = ? AND clip IS NOT NULL", (pid,)).fetchall()
    finally:
        c.close()

    seen, cands = set(), []
    for r in rows:
        key = (r["clip"], r["speaker"])
        if key in seen:
            continue
        seen.add(key)
        t = clip_time(r["clip"])
        wav = os.path.join(pipeline.DATA, r["clip"])
        tp = pipeline.transcript_path(r["clip"])
        if t is None or not (os.path.exists(wav) and os.path.exists(tp)):
            continue
        try:
            tr = json.load(open(tp))
        except Exception:
            continue
        turns = [(float(s["start"]), float(s["end"]))
                 for s in (tr.get("segments") or [])
                 if s.get("speaker") == r["speaker"]
                 and isinstance(s.get("start"), (int, float))
                 and isinstance(s.get("end"), (int, float))
                 and s["end"] > s["start"]]
        total = sum(b - a for a, b in turns)
        if total < MIN_SPAN_SECONDS:
            continue
        cands.append({"clip": r["clip"], "wav": wav, "time": t, "turns": turns})

    # Stratify by calendar day, not evenly over the sorted list.
    #
    # References pile up wherever the labelling happened: 278 of Nathan's 327
    # are from one afternoon. Sampling evenly across the list therefore draws
    # almost everything from that afternoon and reports a span of half a day,
    # which is precisely the comparison this tool exists to avoid making.
    import time as _time
    by_day = collections.defaultdict(list)
    for d in cands:
        by_day[_time.strftime("%Y-%m-%d", _time.localtime(d["time"]))].append(d)
    per_day = max(2, limit // max(1, len(by_day)))
    out = []
    for day in sorted(by_day):
        group = sorted(by_day[day], key=lambda d: d["time"])
        if len(group) > per_day:
            step = len(group) / per_day
            group = [group[int(i * step)] for i in range(per_day)]
        out.extend(group)
    out.sort(key=lambda d: d["time"])
    return out


def load_audio(span):
    import soundfile as sf
    audio, sr = sf.read(span["wav"], dtype="float32")
    if sr != 16000:
        return None
    parts, total = [], 0.0
    for a, b in span["turns"]:
        if total >= MAX_SPAN_SECONDS:
            break
        parts.append(audio[int(a * sr):int(b * sr)])
        total += b - a
    if not parts:
        return None
    joined = np.concatenate(parts)
    return joined if len(joined) >= int(MIN_SPAN_SECONDS * 16000) else None


def _load_model(model_name, dev):
    """Return a callable taking a (1,1,N) float tensor and giving a vector.

    speechbrain is loaded directly rather than through pyannote's wrapper. The
    wrapper forwards a `token` argument that speechbrain 1.1.1 no longer
    accepts, so the two versions installed here cannot be introduced to each
    other that way -- and the wrapper adds nothing this needs.
    """
    if model_name.startswith("speechbrain/"):
        from speechbrain.inference.speaker import EncoderClassifier
        enc = EncoderClassifier.from_hparams(
            source=model_name,
            savedir=os.path.join(ROOT, "data", "models", model_name.split("/")[-1]),
            run_opts={"device": str(dev)})

        def call(t):
            # speechbrain wants (batch, samples) and returns (batch, 1, dim).
            return enc.encode_batch(t.reshape(1, -1)).squeeze()
        return call

    from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding
    model = PretrainedSpeakerEmbedding(model_name, device=dev,
                                       token=os.environ.get("HF_TOKEN"))
    return model


def embed_all(model_name, waves):
    import torch
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = _load_model(model_name, dev)
    out, first_error = [], None
    for w in waves:
        try:
            t = torch.from_numpy(np.asarray(w, dtype=np.float32)).reshape(1, 1, -1).to(dev)
            r = model(t)
            if hasattr(r, "detach"):
                r = r.detach().cpu()
            v = np.asarray(r).ravel().astype(np.float64)
            out.append(store.unit(v) if v.size and np.all(np.isfinite(v)) else None)
        except Exception as e:
            # Report rather than swallow: a model that silently returns nothing
            # for every span would otherwise show up as a blank column and be
            # read as a result.
            if first_error is None:
                first_error = f"{type(e).__name__}: {e}"
            out.append(None)
    if first_error and not any(v is not None for v in out):
        print(f"    all spans failed -- {first_error[:160]}")
    return out


def curve(times, vecs):
    grouped = collections.defaultdict(list)
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if vecs[i] is None or vecs[j] is None:
                continue
            gap = abs(times[i] - times[j])
            sim = float(vecs[i] @ vecs[j])
            for name, limit in BINS:
                if gap < limit:
                    grouped[name].append(sim)
                    break
    return grouped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--person", default="Nathan")
    ap.add_argument("--spans", type=int, default=50)
    a = ap.parse_args()

    spans = gather_spans(a.person, a.spans)
    waves, times, kept = [], [], []
    for sp in spans:
        w = load_audio(sp)
        if w is not None:
            waves.append(w)
            times.append(sp["time"])
            kept.append(sp)
    if len(waves) < 6:
        sys.exit(f"only {len(waves)} usable spans for {a.person}; need more labels")

    span_days = (max(times) - min(times)) / 86400
    print(f"{a.person}: {len(waves)} spans over {span_days:.1f} days, "
          f"{sum(len(w) for w in waves)/16000:.0f}s of audio\n")

    results = {}
    for label, name in MODELS:
        print(f"embedding with {label} …", flush=True)
        results[label] = curve(times, embed_all(name, waves))

    print(f"\n{'time apart':>12}", end="")
    for label, _ in MODELS:
        print(f"{label:>22}", end="")
    print()
    for name, _ in BINS:
        have = [results[l].get(name) for l, _ in MODELS]
        if not any(have):
            continue
        print(f"{name:>12}", end="")
        for vals in have:
            if vals:
                print(f"{np.median(vals):>16.3f} (n={len(vals):>3})", end="")
            else:
                print(f"{'-':>22}", end="")
        print()

    print("\nWhat to look for: the current model falls from about 0.82 within "
          "the hour\nto about 0.52 across days. A model that holds its median "
          "up across the\nbottom rows would remove the reason for most of the "
          "manual labelling.\nOne that falls the same way says the drift is in "
          "the speech or the capture\npath, not the model, and the store's "
          "design is the right response to it.")


if __name__ == "__main__":
    main()
