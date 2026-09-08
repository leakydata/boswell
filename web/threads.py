"""Turning transcript lines back into things a person said.

A thirty-second clip is a transport unit. It cuts wherever thirty seconds
happened to land, which is usually mid-sentence, so one thought arrives as
two or three rows with a speaker label repeated on each. Reading the archive
means reading around that, and anything downstream -- a summary, a quote, a
search hit -- inherits the fragments.

Two joins, deliberately separate because they fail differently:

**Stitching** puts one person's continuing speech back together. It is
mechanical: same speaker, small gap, nothing suspicious in between. No model,
no inference, and every rule below can be checked against the data.

**Sectioning** finds where the subject changes. That is a judgement, and it
uses the sentence embeddings the archive has already computed rather than
asking a model to read anything -- 12,194 of them are sitting in semantic.db
and cost nothing to compare.

Nothing here writes. The callers decide what to do with what it returns.
"""
import math
import os

# How long a silence can be and still be the same person carrying on. Two
# seconds is a breath and a thought; four is the other person deciding not to
# speak. Measured against this archive, 2.0 keeps sentences together across a
# clip boundary without gluing a question to the answer it got.
JOIN_GAP = 2.0

# A unit has to stop somewhere. Without a cap, one person talking steadily --
# reading aloud, or on a call -- becomes a single wall of text with no handle
# on it, which is the problem this is supposed to fix rather than cause.
MAX_SECONDS = 120.0
MAX_WORDS = 150

# How alike two diarized slots must sound to be called one person across a
# clip boundary. The same figure host/speaker_db.py already identifies people
# with, and for the same measured reason: same speaker across recordings
# scores 0.65-0.87, different speakers 0.38-0.48. Measured again on this
# archive's own slots, same-person pairs came out median 0.655, p10 0.505.
#
# Conservative on purpose. Ten percent of genuine same-person pairs fall
# below it and stay split, which costs a join; going lower would eventually
# merge two people into one utterance, which puts words in someone's mouth
# and leaves no trace that it happened.
SAME_VOICE = 0.60


def _words(text):
    return len((text or "").split())


def with_absolute_times(segments, clip_start):
    """Put every line on one timeline.

    Lines carry an offset inside their own clip, so "start 3.2" means nothing
    until you know which clip. Stitching compares the end of one line with the
    start of the next, and across a clip boundary those are offsets into two
    different files -- comparing them directly reads a 28-second gap as if the
    second line came first.

    `clip_start` maps a clip name to when its audio began, in epoch seconds.
    A clip with no known start is left alone rather than guessed at: its lines
    keep their offsets and simply never join across a boundary.
    """
    out = []
    for seg in segments:
        base = clip_start.get(seg.get("clip"))
        s = dict(seg)
        if base is None:
            s["at"] = s["until"] = None
        else:
            s["at"] = base + (seg.get("start") or 0.0)
            s["until"] = base + (seg.get("end") or seg.get("start") or 0.0)
        out.append(s)
    return out


def _cos(a, b):
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / ((na * nb) or 1.0)


def _same_voice(a, b, voices=None):
    """Is this the same person still speaking?

    Diarized ids are per clip. SPEAKER_00 in one clip is not SPEAKER_00 in
    the next -- the server already learned this the hard way, when merging
    the maps put one person's name on three other people's speech. So inside
    one clip the id is the whole answer, and across a clip boundary it means
    nothing at all.

    A resolved name carries across, but only half this archive's lines have
    one: 51% measured, with another 47% holding an id nobody has put a name
    to. Refusing those would leave most sentences split exactly where the
    thirty-second boundary fell, which is the thing this exists to undo.

    The transcripts already carry a 256-dimension voiceprint for every slot,
    which is what the archive identifies people with in the first place. So
    an unnamed slot can still be compared with an unnamed slot, and the
    question stops being a guess and becomes a measurement.
    """
    if a.get("clip") == b.get("clip"):
        return (a.get("speaker") is not None
                and a.get("speaker") == b.get("speaker"))
    an, bn = (a.get("name") or "").strip(), (b.get("name") or "").strip()
    if an and bn:
        return an == bn
    # One named and one not is not evidence of anything on its own -- but if
    # they sound the same it is, and if they do not the name settles it.
    if voices:
        va = voices.get((a.get("clip"), a.get("speaker")))
        vb = voices.get((b.get("clip"), b.get("speaker")))
        if va and vb:
            return _cos(va, vb) >= SAME_VOICE
    return False


def _joinable(prev, seg, gap, max_seconds, max_words, voices=None):
    if not _same_voice(prev, seg, voices):
        return False
    # An attribution the diarizer itself could not hold together is not a
    # basis for merging two lines into one utterance. It is shown with a
    # caveat elsewhere; here it simply ends the run.
    if prev.get("shaky") or seg.get("shaky"):
        return False
    # Two recorders are two rooms. Their clips interleave in time, and
    # joining across them would splice one room's audio into another's.
    if (prev.get("device_id") and seg.get("device_id")
            and prev["device_id"] != seg["device_id"]):
        return False
    a, b = prev.get("until"), seg.get("at")
    if a is None or b is None:
        return False               # no shared timeline, so no measurable gap
    if b - a > gap:
        return False
    # Going backwards means the ordering is not what it looks like -- two
    # clips whose times overlap, or a sidecar written from a drifted clock.
    # Better a short unit than one that reads out of sequence.
    if b - a < -gap:
        return False
    if prev["_seconds"] + max(0.0, (seg.get("until") or 0) - b) > max_seconds:
        return False
    if prev["_words"] + _words(seg.get("text")) > max_words:
        return False
    return True


def stitch(segments, voices=None, gap=JOIN_GAP, max_seconds=MAX_SECONDS,
           max_words=MAX_WORDS):
    """Join consecutive lines that are one person still talking.

    Takes lines already on one timeline (see `with_absolute_times`), in the
    order they were spoken. `voices` maps (clip, speaker id) to that slot's
    voiceprint, and is what lets an unnamed speaker be followed across a clip
    boundary. Returns units, each carrying the lines it was
    built from so nothing is lost: the reader can still jump to the audio for
    any part of it, and an edit can still be applied to the line it belongs
    to.
    """
    units = []
    for seg in segments:
        if units and _joinable(units[-1], seg, gap, max_seconds, max_words,
                               voices):
            u = units[-1]
            u["text"] = (u["text"] + " " + (seg.get("text") or "").strip()).strip()
            u["until"] = seg.get("until") or u["until"]
            u["end"] = seg.get("end", u["end"])
            u["parts"].append(seg)
            u["_words"] += _words(seg.get("text"))
            u["_seconds"] = max(0.0, (u["until"] or 0) - (u["at"] or 0))
            # A name found later in the run names the whole of it. The voice
            # did not change; only what was known about it did.
            if not u.get("name") and seg.get("name"):
                u["name"] = seg["name"]
            continue
        text = (seg.get("text") or "").strip()
        units.append({
            "clip": seg.get("clip"),
            "index": seg.get("index"),
            "start": seg.get("start", 0.0),
            "end": seg.get("end", 0.0),
            "at": seg.get("at"),
            "until": seg.get("until"),
            "text": text,
            "speaker": seg.get("speaker"),
            "name": seg.get("name"),
            "shaky": bool(seg.get("shaky")),
            "device_id": seg.get("device_id"),
            "parts": [seg],
            "_words": _words(text),
            "_seconds": max(0.0, (seg.get("until") or 0) - (seg.get("at") or 0)),
        })
    for u in units:
        u["seconds"] = round(u.pop("_seconds"), 2)
        u["words"] = u.pop("_words")
        u["lines"] = len(u["parts"])
    return units


# ---------------------------------------------------------------- sectioning
def _unit_vector(unit, vectors):
    """One vector for a unit, from the lines inside it that have one.

    Lines under 25 characters are never embedded, so a third of the archive's
    rows have no vector at all. Stitching helps by itself -- a unit is several
    lines, and it only takes one of them to be worth embedding -- but a unit
    of nothing but short lines still has none, and has to be carried along
    without a say in where the boundaries fall.
    """
    got = [vectors.get((p.get("clip"), p.get("index"))) for p in unit["parts"]]
    got = [v for v in got if v]
    if not got:
        return None
    n = len(got[0])
    mean = [sum(v[i] for v in got) / len(got) for i in range(n)]
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return [x / norm for x in mean]


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b))


def _block(vecs, lo, hi):
    """The average direction of a run of units, normalised."""
    got = [v for v in vecs[lo:hi] if v]
    if not got:
        return None
    n = len(got[0])
    mean = [sum(v[i] for v in got) / len(got) for i in range(n)]
    norm = math.sqrt(sum(x * x for x in mean)) or 1.0
    return [x / norm for x in mean]


# A dip has to be a real dip, not just the lowest point of a flat line.
# The cutoff below is relative -- deeper than the average dip -- and a
# conversation that never leaves its subject still has an average, so
# relative scoring alone cut a steady twelve-unit stretch into three. This is
# the floor underneath it: below this the similarity barely moved, and
# whatever the ranking says, nothing happened there.
MIN_DEPTH = 0.08


def sections(units, vectors, window=3, hard_gap=180.0, min_units=4,
             min_depth=MIN_DEPTH):
    """Where the subject changes, from embeddings already stored.

    This is TextTiling, which is old and cheap and does not need a model:
    compare the run of units before each gap with the run after it, and the
    places where that similarity dips are where the subject moved. A dip is
    scored by its depth -- how far it fell from the peaks on either side --
    rather than by its absolute value, because a conversation that stays on
    one subject is uniformly similar and one that wanders is uniformly less
    so, and a fixed threshold would cut the second to ribbons and the first
    not at all.

    A long silence is a boundary whatever the words did. Three minutes of
    nothing is a different sitting, and no amount of similarity across it
    means the two halves belong together.

    Returns a list of sections, each a run of consecutive units.
    """
    if not units:
        return []
    vecs = [_unit_vector(u, vectors) for u in units]

    # Silences first: these are certain, and the scoring below should not get
    # a vote on them.
    forced = set()
    for i in range(1, len(units)):
        a, b = units[i - 1].get("until"), units[i].get("at")
        if a is not None and b is not None and (b - a) >= hard_gap:
            forced.add(i)

    scored = {}
    if len(units) >= min_units:
        sims = []
        for i in range(1, len(units)):
            left = _block(vecs, max(0, i - window), i)
            right = _block(vecs, i, min(len(vecs), i + window))
            sims.append(None if (left is None or right is None)
                        else _cosine(left, right))
        # Depth of each valley: how far it sits below the nearest higher
        # ground on each side. A gap with no similarity to score is skipped
        # rather than treated as a dip -- absent is not low.
        depths = {}
        for k, s in enumerate(sims):
            if s is None:
                continue
            left = s
            j = k - 1
            while j >= 0 and sims[j] is not None and sims[j] >= left:
                left = sims[j]
                j -= 1
            right = s
            j = k + 1
            while j < len(sims) and sims[j] is not None and sims[j] >= right:
                right = sims[j]
                j += 1
            depths[k + 1] = (left - s) + (right - s)
        if depths:
            vals = list(depths.values())
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            # TextTiling's own cutoff: deeper than average by half a standard
            # deviation. Liberal on purpose -- a section too many is a heading
            # in the wrong place, a section too few hides one thing inside
            # another.
            cut = max(mean + math.sqrt(var) / 2, min_depth)
            scored = {i: d for i, d in depths.items() if d > cut}

    breaks = sorted(forced | set(scored))
    out, start = [], 0
    for b in breaks + [len(units)]:
        if b <= start:
            continue
        run = units[start:b]
        out.append({
            "units": run,
            "at": run[0].get("at"),
            "until": run[-1].get("until"),
            "seconds": round(sum(u.get("seconds") or 0 for u in run), 2),
            "words": sum(u.get("words") or 0 for u in run),
            "speakers": list(dict.fromkeys(
                [u["name"] for u in run if u.get("name")])),
            "why": ("silence" if start in forced else
                    ("subject" if start in scored else "start")),
        })
        start = b
    return out


# ------------------------------------------------------- reading the archive
def _transcript_path(clip):
    """Where a clip's transcript lives.

    Worked out here rather than imported from pipeline, which pulls in torch
    -- twelve seconds and a CUDA probe to learn a filename. A read-only pass
    over transcripts should not need the machinery that wrote them.

    The bare-filename check is pipeline's and is kept: this takes a name from
    a request, and a name carrying a path would read outside the store.
    """
    import os
    name = os.path.basename(clip)
    if name != clip or not name:
        raise ValueError(f"clip name must be a bare filename: {clip!r}")
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "data", "transcripts",
                        os.path.splitext(name)[0] + ".json")


def _voiceprints(transcript):
    """Every diarized slot's voiceprint, keyed the way stitch() asks for it."""
    return {k: v for k, v in (transcript.get("embeddings") or {}).items() if v}


def for_conversation(names):
    """Load one conversation's lines, stitch them, and section the result.

    Reads only. Imports are local so the logic above stays importable without
    the archive behind it -- the tests exercise the rules, not the disk.
    """
    import json
    import os
    import index_db
    import semantic

    segments, clip_start, voices = [], {}, {}
    for name in names:
        tp = _transcript_path(name)
        if not os.path.exists(tp):
            continue
        try:
            t = json.load(open(tp))
        except Exception:
            continue

        # The device's own account of when the audio began, which is the only
        # witness that was actually there. mtime is the end of the clip and
        # gets rewritten by a copy or a backup.
        times = index_db.device_times(name)
        if times:
            clip_start[name] = times[0]

        sp = t.get("speakers") or {}
        for sid, v in _voiceprints(t).items():
            voices[(name, sid)] = v

        dev = index_db.device_of(name)
        for i, seg in enumerate(t.get("segments", [])):
            sid = seg.get("speaker")
            rec = sp.get(sid) or {}
            segments.append({
                "clip": name, "index": i,
                "start": seg.get("start", 0.0), "end": seg.get("end", 0.0),
                "text": seg.get("text", ""),
                "speaker": sid,
                "name": seg.get("speaker_name") or rec.get("name"),
                "shaky": bool(rec.get("impure")) and not seg.get("speaker_name"),
                "device_id": dev,
            })

    # In the order they were spoken, which is not the order the clips were
    # named or filed: recovered audio arrives late and belongs where it
    # happened.
    segments = with_absolute_times(segments, clip_start)
    segments.sort(key=lambda s: (s["at"] if s["at"] is not None else 0,
                                 s["clip"], s["index"]))

    units = stitch(segments, voices=voices)

    # The sentence vectors the archive already computed. Nothing is embedded
    # here: a line too short to have been worth embedding stays without one.
    vectors = {}
    try:
        db, _ = semantic._connect()
        qs = ",".join("?" * len(names)) or "''"
        for r in db.execute(
                f"SELECT clip, idx, vec FROM seg WHERE clip IN ({qs})",
                list(names)):
            if r["vec"]:
                vectors[(r["clip"], r["idx"])] = semantic._unpack(r["vec"])
    except Exception:
        pass

    return {"units": units, "sections": sections(units, vectors)}


def main():
    """A pass over the archive, for a cron entry or a look by hand.

    Reports rather than writes. Reading is fast enough -- eleven seconds for
    the whole archive -- that the interface computes this per request, so
    there is nothing here to keep up to date. What this is for is seeing what
    the joins are doing across everything at once, which is how the rules get
    checked against the archive rather than against six examples.
    """
    import argparse
    import sys
    import time
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import index_db

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=2000,
                    help="how many clips back to group into conversations")
    ap.add_argument("--device", help="only one recorder")
    ap.add_argument("--show", type=int, default=0,
                    help="print this many units from the newest conversation")
    args = ap.parse_args()

    t0 = time.time()
    groups = index_db.conversations(limit=args.limit, device=args.device)
    lines = units = secs = 0
    for g in groups:
        r = for_conversation(g["clips"])
        lines += sum(u["lines"] for u in r["units"])
        units += len(r["units"])
        secs += len(r["sections"])
    took = time.time() - t0
    saved = 100 * (1 - units / max(lines, 1))
    print(f"{len(groups)} conversation(s) in {took:.1f}s")
    print(f"  {lines} line(s) -> {units} unit(s)  ({saved:.0f}% fewer)")
    print(f"  {secs} section(s)")

    if args.show and groups:
        r = for_conversation(groups[0]["clips"])
        print(f"\nnewest conversation, first {args.show} unit(s):")
        for u in r["units"][:args.show]:
            who = u.get("name") or u.get("speaker") or "?"
            print(f"  [{who}] {u['lines']} line(s), {u['seconds']}s")
            print(f"    {u['text'][:150]}")


if __name__ == "__main__":
    main()
