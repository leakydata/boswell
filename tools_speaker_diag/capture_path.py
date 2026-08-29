#!/usr/bin/env python3
"""Is the drift in the voice, or in the microphone?

This is the last question standing. A voice scores 0.86 against itself within
the hour and about 0.5 a day later, and a second embedder trained on different
data falls the same way -- so the collapse is not the model. It is either
genuine day-to-day variation in how a person sounds, or it is the capture path:
a PDM microphone on a wearable, ADPCM over BLE, worn slightly differently every
morning.

The two have opposite consequences, which is why it matters:

  the voice     nothing fixes it. The store's design -- one reference per
                condition, name people again tomorrow -- is the permanent
                answer, and the daily queue is the product.

  the capture   fixable in hardware or firmware, and worth fixing, because it
                would recover cross-day identity and remove most of the manual
                labelling.

This control has been on the open list in two projects and has never been
recorded, which after twice is a fact about the protocol rather than about the
priority. So the protocol here is small enough to actually do.

------------------------------------------------------------------- protocol

Twice, on two different days, at least a day apart. Five minutes each time.

  1. Wear the device as you normally would. This machine already has a usable
     clean microphone: the C922 webcam on card 1. `arecord -l` if that ever
     changes.

  2. Start the USB recording. From the repository root:

         mkdir -p data/control
         arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 \
                 data/control/clean_$(date +%s).wav

     Ctrl-C stops it. Leave the wearable doing what it always does; its clips
     land in data/ by themselves and this tool finds them by timestamp.

  3. Read anything for about ninety seconds. The same passage both days is
     tidier but not required -- this measures whether a voice matches itself,
     not what it said.

  4. Stop the USB recording. Note roughly when you finished, so the wearable
     clips covering that window can be found.

  5. Second day, repeat. Different room if convenient; that is the point.

Then:

    uv run tools_speaker_diag/capture_path.py

It reads data/control/clean_*.wav for the clean side and takes the wearable
side from the archive's own clips at matching times, so nothing needs labelling
by hand.

-------------------------------------------------------------------- reading

Both columns falling together says the drift is in the voice and the capture
path is exonerated. The clean column holding up while the wearable column falls
says it is the microphone, and 8 kHz-origin ADPCM through a PDM mic becomes the
thing to fix.

A clean column that holds up is the good outcome, and the expensive one.
"""
import argparse
import collections
import glob
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "web"))

import speaker_store as store         # noqa: E402

CONTROL = os.path.join(ROOT, "data", "control")
MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"
WINDOW = 15.0            # seconds per embedding
MIN_WINDOWS = 4


def stamp(path):
    m = re.search(r"(\d{9,})", os.path.basename(path))
    return int(m.group(1)) if m else None


def windows(path):
    """Chop one recording into fixed windows and hand back (time, audio).

    Fixed windows rather than whole files so a ninety-second reading and a
    thirty-second clip contribute comparable evidence -- an embedding pooled
    over three times as much audio is a steadier vector, and comparing one
    against the other would measure that rather than the microphone.
    """
    import soundfile as sf
    try:
        audio, sr = sf.read(path, dtype="float32")
    except Exception:
        return []
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != 16000 or len(audio) < int(WINDOW * sr):
        return []
    base = stamp(path) or 0
    out, n = [], int(WINDOW * sr)
    for i in range(0, len(audio) - n + 1, n):
        seg = audio[i:i + n]
        if float(np.sqrt(np.mean(seg ** 2))) < 1e-4:
            continue          # silence embeds to noise
        out.append((base + i / sr, seg))
    return out


def collect_clean():
    return [w for p in sorted(glob.glob(os.path.join(CONTROL, "clean_*.wav")))
            for w in windows(p)]


def collect_wearable(times, tolerance=900):
    """Wearable clips recorded around the same moments as the clean sessions.

    Matched by time rather than by content, so nothing has to be labelled: if
    the device was on your neck while you read into the USB microphone, its
    clips from that window are the same speech through the other path.
    """
    sessions = []
    for t in times:
        if not sessions or abs(t - sessions[-1]) > tolerance:
            sessions.append(t)
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "data", "*.wav"))):
        t = stamp(p)
        if t is None:
            continue
        if any(abs(t - s) <= tolerance for s in sessions):
            out.extend(windows(p))
    return out


def embed(waves):
    import torch
    from pyannote.audio.pipelines.speaker_verification import PretrainedSpeakerEmbedding
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = PretrainedSpeakerEmbedding(MODEL, device=dev,
                                       token=os.environ.get("HF_TOKEN"))
    out = []
    for _, w in waves:
        try:
            t = torch.from_numpy(np.asarray(w, dtype=np.float32)).reshape(1, 1, -1).to(dev)
            v = np.asarray(model(t)).ravel().astype(np.float64)
            out.append(store.unit(v) if v.size and np.all(np.isfinite(v)) else None)
        except Exception:
            out.append(None)
    return out


def curve(waves, vecs):
    same_day, across = [], []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            if vecs[i] is None or vecs[j] is None:
                continue
            gap = abs(waves[i][0] - waves[j][0])
            sim = float(vecs[i] @ vecs[j])
            (same_day if gap < 6 * 3600 else across).append(sim)
    return same_day, across


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tolerance", type=int, default=900,
                    help="seconds either side of a clean session to pull "
                         "wearable clips from")
    a = ap.parse_args()

    clean = collect_clean()
    if len(clean) < MIN_WINDOWS:
        sys.exit(
            f"found {len(clean)} usable window(s) in {CONTROL}.\n"
            "Record the control first -- the protocol is in this file's "
            "docstring, and it is two five-minute sessions on different days.")
    times = sorted(t for t, _ in clean)
    if (max(times) - min(times)) < 20 * 3600:
        print("WARNING: all the clean audio is from one session. This can only "
              "measure\nsame-day agreement until there is a second day, which "
              "is the whole question.\n")

    wear = collect_wearable(times, a.tolerance)
    print(f"clean mic : {len(clean)} windows over "
          f"{(max(times) - min(times)) / 86400:.1f} days")
    print(f"wearable  : {len(wear)} windows matched by time")
    if len(wear) < MIN_WINDOWS:
        print("\nNot enough wearable audio around those times. Was the device "
              "recording?\nRaise --tolerance if the clocks disagree.")

    print(f"\n{'source':>12} {'same day':>18} {'across days':>18}")
    for label, waves in (("clean mic", clean), ("wearable", wear)):
        if len(waves) < MIN_WINDOWS:
            print(f"{label:>12} {'-- too little audio --':>38}")
            continue
        s, x = curve(waves, embed(waves))
        sd = f"{np.median(s):.3f} (n={len(s)})" if s else "-"
        ac = f"{np.median(x):.3f} (n={len(x)})" if x else "-"
        print(f"{label:>12} {sd:>18} {ac:>18}")

    print("\nBoth falling together: the drift is in the voice, the capture path "
          "is exonerated,\nand the store's design is the permanent answer. The "
          "clean column holding up\nwhile the wearable falls: it is the "
          "microphone, and that is worth fixing.")


if __name__ == "__main__":
    main()
