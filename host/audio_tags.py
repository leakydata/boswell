#!/usr/bin/env python3
"""What is actually in the clips that hold no transcript?

Roughly half the archive by size is clips the transcriber found no speech in --
1380 of 2517, 1.26 GB. The obvious thing to do with them is delete them, and
the obvious thing is wrong: sampling sixty of them and asking an audio tagger
what they contain turned up speech the transcriber had missed, and a turkey.

    clip_1787264540   Speech 0.57                          <- missed speech
    clip_1787262406   Turkey 0.72, Fowl 0.58, Gobble 0.50  <- a real event
    clip_1787262316   White noise 0.20                     <- nothing
    clip_1787257987   Wind noise (microphone) 0.23         <- nothing

So one in five of the loud ones held something worth keeping, and "no transcript"
turns out to mean "Whisper and pyannote agreed there was nothing", which is a
weaker claim than it sounds. This asks a third model, trained on a different
task, before anything is deleted.

The model is AST fine-tuned on AudioSet -- 527 everyday sound classes, dog barks
and music and wind among them. Nothing needs fine-tuning for that; the classes
already exist. It runs from transformers, which is already installed here, so it
adds no dependency.

    uv run host/audio_tags.py                      # report, delete nothing
    uv run host/audio_tags.py --tag Music --tag Television
    uv run host/audio_tags.py --deletable          # list what is safe to remove
    uv run host/audio_tags.py --deletable --delete # actually remove those

Nothing is deleted without --delete, and --delete only ever touches clips this
tool has itself examined and found empty by every test it has.
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "web"))

import pipeline                       # noqa: E402

MODEL = "MIT/ast-finetuned-audioset-10-10-0.4593"

# Anything in this family means a person was audible, whatever the transcriber
# concluded. A clip carrying one is never a deletion candidate.
VOICE = {"Speech", "Male speech, man speaking", "Female speech, woman speaking",
         "Child speech, kid speaking", "Conversation", "Narration, monologue",
         "Whispering", "Shout", "Yell", "Screaming", "Laughter", "Crying, sobbing",
         "Singing", "Speech synthesizer"}

# Present in almost every recording and not evidence of content.
AMBIENT = {"Silence", "White noise", "Pink noise", "Wind noise (microphone)",
           "Wind", "Mechanical fan", "Air conditioning", "Hum", "Mains hum",
           "Static", "Noise", "Environmental noise", "Inside, small room",
           "Inside, large room or hall", "Rustling leaves", "Vehicle",
           "Tick", "Tick-tock", "Clock"}

VOICE_FLOOR = 0.10        # a whisper of speech is enough to keep a clip
EVENT_FLOOR = 0.35        # a named non-ambient event worth knowing about


def load_model():
    import torch
    from transformers import AutoFeatureExtractor, AutoModelForAudioClassification
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    fe = AutoFeatureExtractor.from_pretrained(MODEL)
    m = AutoModelForAudioClassification.from_pretrained(MODEL).to(dev).eval()
    return fe, m, dev, m.config.id2label


def tag(path, fe, model, dev, labels, topk=6):
    import soundfile as sf
    import torch
    try:
        a, sr = sf.read(path, dtype="float32")
    except Exception:
        return None
    if a.ndim > 1:
        a = a.mean(axis=1)
    if not len(a):
        return None
    rms = float(np.sqrt(np.mean(a ** 2)))
    # The older clips are 8 kHz -- ADPCM through the early firmware -- and the
    # model wants 16. Resampled rather than skipped: those are the recordings
    # most likely to have had speech missed in the first place, so excluding
    # them would exclude exactly the ones this exists to check.
    if sr != 16000:
        import torchaudio
        a = torchaudio.functional.resample(
            torch.from_numpy(np.ascontiguousarray(a)), sr, 16000).numpy()
        sr = 16000
    with torch.no_grad():
        x = fe(a, sampling_rate=sr, return_tensors="pt").to(dev)
        p = torch.sigmoid(model(**x).logits[0]).cpu()
    top = torch.topk(p, topk)
    return {"rms_db": 20 * np.log10(rms + 1e-12),
            "tags": [(labels[i.item()], float(v)) for v, i in
                     zip(top.values, top.indices)]}


def verdict(r):
    """keep | empty, and why. Deliberately biased towards keeping.

    Deleting a recording is irreversible and the whole point of the device is
    that it was there when you were not paying attention. So anything that
    looks like a voice keeps the clip, anything with a named non-ambient event
    keeps the clip, and only a clip whose entire top-six is ambient is called
    empty.
    """
    voice = [(n, v) for n, v in r["tags"] if n in VOICE and v >= VOICE_FLOOR]
    if voice:
        return "keep", f"voice: {voice[0][0]} {voice[0][1]:.2f}"
    events = [(n, v) for n, v in r["tags"]
              if n not in AMBIENT and n not in VOICE and v >= EVENT_FLOOR]
    if events:
        return "keep", f"event: {', '.join(f'{n} {v:.2f}' for n, v in events[:2])}"
    return "empty", f"only ambient ({r['tags'][0][0]} {r['tags'][0][1]:.2f})"


def candidates():
    """Clips the transcriber found nothing in. The starting set, not the answer."""
    data = os.path.join(ROOT, "data")
    out = []
    for f in sorted(os.listdir(data)):
        if not f.endswith(".wav"):
            continue
        tp = pipeline.transcript_path(f)
        if not os.path.exists(tp):
            continue          # never examined; not this tool's business
        try:
            t = json.load(open(tp))
        except Exception:
            continue
        if not (t.get("segments") or []):
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--tag", action="append", default=[],
                    help="report clips carrying this AudioSet label, e.g. --tag Music")
    ap.add_argument("--deletable", action="store_true",
                    help="list only the clips found empty by every test")
    ap.add_argument("--delete", action="store_true",
                    help="actually remove them. Requires --deletable.")
    a = ap.parse_args()
    if a.delete and not a.deletable:
        sys.exit("--delete only works with --deletable, so the list is seen first")

    todo = candidates()
    print(f"{len(todo)} clip(s) hold no transcript; examining "
          f"{min(a.limit, len(todo))}\n")
    todo = todo[:a.limit]

    fe, model, dev, labels = load_model()
    keep, empty, wanted = [], [], []
    data = os.path.join(ROOT, "data")
    for i, f in enumerate(todo, 1):
        r = tag(os.path.join(data, f), fe, model, dev, labels)
        if r is None:
            continue
        v, why = verdict(r)
        (empty if v == "empty" else keep).append((f, why, r))
        for want in a.tag:
            if any(n == want and s >= 0.10 for n, s in r["tags"]):
                wanted.append((f, want, dict(r["tags"])[want]))
        if i % 25 == 0:
            print(f"  … {i}/{len(todo)}", flush=True)

    print(f"\nkeep : {len(keep)}   empty: {len(empty)}")
    print("\nthe ones worth keeping, and why:")
    for f, why, _ in keep[:20]:
        print(f"  {f:<36} {why}")
    if len(keep) > 20:
        print(f"  … and {len(keep) - 20} more")

    if a.tag:
        print(f"\ncarrying {', '.join(a.tag)}:")
        for f, t, s in sorted(wanted, key=lambda x: -x[2])[:25]:
            print(f"  {f:<36} {t} {s:.2f}")
        if not wanted:
            print("  none")

    if a.deletable:
        size = sum(os.path.getsize(os.path.join(data, f)) for f, _, _ in empty)
        print(f"\n{len(empty)} clip(s) found empty by every test, {size/1e6:.0f} MB")
        for f, why, _ in empty[:15]:
            print(f"  {f:<36} {why}")
        if len(empty) > 15:
            print(f"  … and {len(empty) - 15} more")
        if not a.delete:
            print("\nNothing deleted. Add --delete to remove exactly these.")
            return
        for f, _, _ in empty:
            for p in (os.path.join(data, f), pipeline.transcript_path(f),
                      os.path.join(data, "times", f + ".json")):
                try:
                    os.remove(p)
                except OSError:
                    pass
        print(f"\nremoved {len(empty)} clip(s). Run the index rebuild in the "
              f"interface, or index_db.sync(), so the list agrees with the disk.")


if __name__ == "__main__":
    main()
