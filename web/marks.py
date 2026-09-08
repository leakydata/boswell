"""What somebody marked on the recorder, and what they said around it.

A press on the Omi is a timestamp and nothing else. Turning it into something
readable means finding the speech near that moment -- and where "near" falls
is not a guess, it was measured.

Tapped at 00:04:44, 00:04:49 and 00:04:54. The words:

    00:04:17   "This is a test bookmark."               -27.3 s
    00:04:19   "A test bookmark to see what happens."   -25.2 s
    00:04:38   "This is a test."                         -6.4 s
    00:04:40   "This is a test bookmark."                -4.6 s

Every one of them *before* the first press, and nothing at all after it.
Reaching for a button is what people do once the thought has landed, so a
window that only looks forward captures an empty excerpt and looks broken.
Hence a window that reaches well back, and only a little forward.

The clip boundary that day fell at 00:04:41, three seconds before the first
tap -- so the taps landed in a different clip from every word they were
about. Anything anchored on "the clip the press happened in" would have
marked a clip containing none of it. The anchor is the time.
"""
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
MOMENTS = os.path.join(DATA, "moments.jsonl")

# How far either side of a press to look. Asymmetric on purpose, and both
# numbers come from the measurement above: 27 seconds back was needed to
# reach the first sentence, so 60 leaves room for a longer thought. Forward
# stays small but non-zero, because sometimes the press comes first and
# including it costs nothing -- the transcript is there either way.
BEFORE = 60.0
AFTER = 30.0

# A gap this long is somebody else's sentence, not more of this one. Walking
# outward from the press and stopping here gives the excerpt a real edge
# rather than ending it wherever the window happened to fall.
PAUSE = 10.0

# What the gestures mean. Single is a bookmark -- something to come back to.
# Double is a reminder -- something to do. Long press is the firmware
# powering itself off and is not a mark at all; it is in the log so that
# "did I turn it off?" has an answer.
KINDS = {"tap": "bookmark", "double tap": "reminder"}


def load(path=None):
    """Every press, oldest first. A malformed line is skipped, not fatal."""
    out = []
    try:
        with open(path or MOMENTS) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("at"):
                    out.append(d)
    except FileNotFoundError:
        return []
    out.sort(key=lambda m: m["at"])
    return out


def _candidate_clips(at, index_db):
    """Clips whose audio could overlap the window around `at`.

    Narrowed on `modified` first, which is indexed and is the end of the
    clip, then confirmed against the sidecar. Reading a sidecar per clip
    across the whole archive to answer one press would be a file read per
    clip; this reads a handful.
    """
    lo, hi = at - BEFORE - 60, at + AFTER + 60
    rows = []
    for c in index_db.list_clips(4000):
        m = c.get("modified") or 0
        if not (lo <= m <= hi):
            continue
        t = index_db.device_times(c["name"])
        s, e = t if t else (m - (c.get("seconds") or 0), m)
        if s < at + AFTER and e > at - BEFORE:
            rows.append((s, e, c["name"]))
    rows.sort()
    return rows


def _trim_to_pause(units, at):
    """Keep the run of speech the press belongs to, and drop the rest.

    Walks out from the press in both directions and stops at the first real
    silence, so the excerpt ends where the talking ended rather than where
    the window did.
    """
    if not units:
        return []
    timed = [u for u in units if u.get("at") is not None]
    if not timed:
        return units
    # The unit nearest the press is the one it is about.
    nearest = min(range(len(timed)),
                  key=lambda i: min(abs(timed[i]["at"] - at),
                                    abs((timed[i].get("until") or
                                         timed[i]["at"]) - at)))
    lo = hi = nearest
    while lo > 0:
        prev = timed[lo - 1]
        if timed[lo]["at"] - (prev.get("until") or prev["at"]) > PAUSE:
            break
        lo -= 1
    while hi < len(timed) - 1:
        nxt = timed[hi + 1]
        if nxt["at"] - (timed[hi].get("until") or timed[hi]["at"]) > PAUSE:
            break
        hi += 1
    return timed[lo:hi + 1]


def around(moment, index_db=None, threads=None):
    """One press, with the speech it was about.

    Returns the press, the clips the excerpt came from, and the joined
    utterances -- joined, because a thirty-second clip cuts mid-sentence and
    a reminder that stops halfway is not a reminder.
    """
    if index_db is None:
        import index_db as index_db
    if threads is None:
        import threads as threads

    at = moment["at"]
    rows = _candidate_clips(at, index_db)
    names = [n for _, _, n in rows]
    units = threads.for_conversation(names)["units"] if names else []
    inside = [u for u in units
              if u.get("at") is not None
              and u["at"] < at + AFTER and (u.get("until") or u["at"]) > at - BEFORE]
    kept = _trim_to_pause(inside, at)
    return {
        "at": at,
        "kind": moment.get("kind"),
        "means": KINDS.get(moment.get("kind")),
        "device_id": moment.get("device_id"),
        "text": " ".join(u["text"] for u in kept).strip(),
        "units": kept,
        "clips": sorted({u["clip"] for u in kept if u.get("clip")}),
        # How the excerpt sits around the press, so the reader can see
        # whether it was said before or after -- and so this stops being a
        # thing anybody has to assume.
        "starts_at": round(kept[0]["at"] - at, 1) if kept else None,
    }


def recent(limit=50, kind=None, index_db=None, threads=None):
    """Marks, newest first, each with what was said around it."""
    ms = [m for m in load() if m.get("kind") in KINDS]
    if kind:
        want = [k for k, v in KINDS.items() if kind in (k, v)]
        ms = [m for m in ms if m.get("kind") in want]
    ms.reverse()
    return [around(m, index_db, threads) for m in ms[:max(0, limit)]]
