#!/usr/bin/env python3
"""Bring a docked card's recordings into the archive.

    uv run host/ingest_card.py /media/you/BOSWELL          # see what it would do
    uv run host/ingest_card.py /media/you/BOSWELL --write  # actually do it

Three things this has to get right, and each of them is a way to quietly
corrupt an archive rather than to fail loudly:

**Verify before trusting.** A file is only ingested once its header parses,
its length chain lands exactly on the end of the file, and its records check
out. The realistic failures are not scrambled bytes but incomplete ones -- a
cable pulled mid-copy, a battery gone mid-batch -- and those all look like a
file that is shorter than it claims. A file that fails is left alone and
retried next time, which is the whole reason the device does not delete on
upload.

**Do not ingest the same audio twice.** The same thirty seconds can arrive
live over the radio and again in the file, and a conversation that happened
twice reads as a real event rather than as an error. web/dedup.py answers
that from (boot_id, device_ms), which is why the device puts boot_id in every
file header.

**Do not claim to know a time that is not known.** A file whose boot_id is
zero holds audio captured before the device's current run -- its device
milliseconds belong to a run that has ended and cannot be compared with
anything. Those clips are written with their span recorded but flagged, not
placed against the wrong wall-clock moment.
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))

import numpy as np
import soundfile as sf

from read_card import read_file, BadFile
import dedup

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
TIMES = os.path.join(DATA, "times")
LEDGER = os.path.join(DATA, "card_ingested.json")

# Match the live path's granularity. Longer clips would mean re-ingesting
# more when only part of a file is new; shorter would multiply the index.
CLIP_SECONDS = 30


def load_ledger():
    try:
        with open(LEDGER) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_ledger(led):
    os.makedirs(DATA, exist_ok=True)
    tmp = LEDGER + ".part"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LEDGER)


def ledger_key(path):
    """Identity of a file as it sits on the card.

    Size is part of it because the device appends: a file seen at 300 KB and
    ingested, then grown to 600 KB before the next dock, is not the same file
    and its second half has never been read.
    """
    st = os.stat(path)
    return f"{os.path.basename(path)}:{st.st_size}"


def held_records():
    """Every (boot_id, span) the archive already holds."""
    out = []
    if not os.path.isdir(TIMES):
        return out
    for name in os.listdir(TIMES):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(TIMES, name)) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def clips_from(hdr):
    """Split a file's decoded audio into clip-sized pieces with their spans.

    Yields (pcm, first_ms, last_ms). Frames are 20 ms and the device stamps
    each one, so the span comes from the frames rather than from the audio
    length -- a gated recording has gaps, and measuring by samples would
    report a span shorter than the wall time it covers.
    """
    rate = hdr["rate"]
    per_clip = int(CLIP_SECONDS * rate)

    pcm, first, last = [], None, None
    have = 0
    for samples, t_ms in zip(hdr["samples"], hdr["frame_ms"]):
        if first is None:
            first = t_ms
        last = t_ms
        pcm.append(samples)
        have += len(samples)
        if have >= per_clip:
            yield np.concatenate(pcm), first, last
            pcm, first, last, have = [], None, None, 0
    if pcm:
        yield np.concatenate(pcm), first, last


def write_clip(audio, rate, when, source):
    os.makedirs(DATA, exist_ok=True)
    name = f"{source}_{int(when)}"
    path = os.path.join(DATA, name + ".wav")
    n = 1
    while os.path.exists(path):
        path = os.path.join(DATA, f"{name}-{n}.wav")
        n += 1
    tmp = path + ".part"
    try:
        sf.write(tmp, audio, rate, format="WAV", subtype="PCM_16")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def write_times(path, boot_id, first_ms, last_ms, seconds, started, ended,
                placed, device_id=None):
    os.makedirs(TIMES, exist_ok=True)
    rec = {"name": os.path.basename(path),
           "started": round(started, 3), "ended": round(ended, 3),
           "seconds": round(seconds, 3), "source": "card",
           "device_ms": [first_ms, last_ms], "boot_id": boot_id,
           # Which recorder. Absent on everything written before this
           # existed, and dedup reads absent as "could be any", so the
           # archive as it stands keeps behaving exactly as it did.
           "device_id": device_id,
           # False when the file could not say when this happened, so the
           # times above are a placement of last resort rather than a fact.
           "time_known": placed}
    tmp = os.path.join(TIMES, os.path.basename(path) + ".json")
    with open(tmp + ".part", "w") as f:
        json.dump(rec, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp + ".part", tmp)
    return rec


def ingest_file(path, held, ledger, write=False):
    """Verify one file and bring in whatever the archive does not already have.

    Returns a one-line report.
    """
    key = ledger_key(path)
    if key in ledger:
        return f"{os.path.basename(path)}: already ingested"

    try:
        hdr = read_file(path, want_audio=True)
    except BadFile as e:
        # Left on the card, deliberately. Nothing deletes it, so the next
        # dock tries again -- which is the whole argument against deleting on
        # upload.
        return f"{os.path.basename(path)}: UNREADABLE ({e}) -- left on the card"

    if hdr["corrupt"]:
        # Individual records failed their checksum. The rest of the file is
        # still readable, so this is a note rather than a refusal.
        note = f", {hdr['corrupt']} record(s) failed checksum and were skipped"
    else:
        note = ""

    if not hdr["samples"]:
        return f"{os.path.basename(path)}: no decodable audio{note}"

    boot_id = hdr["boot_id"] or None
    device_id = hdr.get("device_id")
    epoch = hdr["boot_epoch"]

    kept = skipped = 0
    for pcm, first_ms, last_ms in clips_from(hdr):
        span = (first_ms, last_ms)

        # Only ask when there is something to ask with. A file with no boot
        # id holds audio from a run that has ended: its device milliseconds
        # cannot be compared with anything the archive holds, so it is kept
        # rather than matched. Erring toward keeping audio is the right way
        # round -- a duplicate can be merged later, a discard cannot.
        if boot_id is not None and dedup.is_duplicate(boot_id, span, held,
                                                      device_id=device_id):
            skipped += 1
            continue

        seconds = len(pcm) / float(hdr["rate"])
        if epoch:
            started = epoch + first_ms / 1000.0
            ended = epoch + last_ms / 1000.0
            placed = True
        else:
            # No clock was ever set for this run. Rather than invent a time,
            # the clip is stamped now and flagged so nothing downstream reads
            # it as a fact about when it happened.
            started = time.time()
            ended = started + seconds
            placed = False

        kept += 1
        if not write:
            continue

        out = write_clip(pcm, hdr["rate"], ended, "card")
        rec = write_times(out, boot_id, first_ms, last_ms, seconds,
                          started, ended, placed, device_id=device_id)
        try:
            os.utime(out, (ended, ended))
        except OSError:
            pass
        # So the next clip in this same file is matched against it too.
        held.append(rec)

    if write:
        ledger[key] = {"at": int(time.time()), "clips": kept,
                       "boot_id": boot_id, "device_id": device_id}

    when = "" if not epoch else time.strftime(
        " %Y-%m-%d %H:%M", time.localtime(epoch + (hdr["first_ms"] or 0) / 1000))
    return (f"{os.path.basename(path)}:{when} {kept} new, {skipped} already "
            f"held{note}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="the mounted card, or its boswell directory")
    ap.add_argument("--write", action="store_true",
                    help="actually import; without this it only reports")
    args = ap.parse_args()

    root = args.path
    if os.path.isdir(os.path.join(root, "boswell")):
        root = os.path.join(root, "boswell")

    files = sorted(os.path.join(root, n) for n in os.listdir(root)
                   if n.endswith(".bwl"))
    if not files:
        print(f"no recordings in {root}")
        return 1

    ledger = load_ledger()
    held = held_records()
    print(f"{len(files)} file(s), archive holds {len(held)} clip(s)")
    if not args.write:
        print("(dry run -- pass --write to import)")

    for p in files:
        print("  " + ingest_file(p, held, ledger, write=args.write))

    if args.write:
        save_ledger(ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
