#!/usr/bin/env python3
"""Do the voice embeddings group by PERSON, or by acoustic CONDITION?

The archive scores held-out voices on a continuum from 0.85 down through 0.74
with no gap anywhere, which says the embeddings are not separating something.
It does not say what. Two very different causes produce that same smear:

  * the embedder encodes WHO is speaking, but weakly -- more enrolment fixes it
  * the embedder encodes WHERE and HOW they were recorded -- nothing about the
    datastore fixes it, and the clean-microphone control becomes the next step

This tells them apart without needing labels, using time as the instrument. A
cluster that is genuinely a person recurs across sessions and days. A cluster
that is really a room, a mic position or a background noise floor is confined
to the session that produced it.
"""
import json
import glob
import os
import sys
from collections import defaultdict

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.spatial.distance import squareform

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get(
    "BOSWELL_ROOT", os.path.normpath(os.path.join(HERE, "..")))
TR = os.path.join(ROOT, "data", "transcripts")
SPK = os.path.join(ROOT, "data", "speakers.json")

# A pause longer than this starts a new session: a different room, a different
# time of day, the device re-seated. This is the axis the test turns on.
SESSION_GAP = 15 * 60
# Below this there is not enough speech for the embedding to mean much, and
# including them measures the noise floor rather than the question.
MIN_SECONDS = 3.0


def load():
    rows = []
    for p in sorted(glob.glob(os.path.join(TR, "*.json"))):
        try:
            j = json.load(open(p))
        except Exception:
            continue
        emb = j.get("embeddings") or {}
        if not emb:
            continue
        clip = j.get("clip") or os.path.basename(p).replace(".json", ".wav")
        try:
            ts = int(clip.replace("clip_", "").replace(".wav", ""))
        except ValueError:
            continue
        named = {k: (v or {}).get("name") for k, v in (j.get("speakers") or {}).items()}
        # Segments carry the RESOLVED name once a speaker has been identified
        # -- tools_reidentify rewrites seg["speaker"] in place -- so keying
        # durations by the raw SPEAKER_xx label finds nothing for exactly the
        # voices that were named, and a duration filter then drops them. Map
        # names back to their diarizer label before counting.
        back = {v: k for k, v in named.items() if v}
        secs = defaultdict(float)
        for s in (j.get("segments") or []):
            who = s.get("speaker")
            who = back.get(who, who)
            try:
                secs[who] += float(s.get("end", 0)) - float(s.get("start", 0))
            except (TypeError, ValueError):
                pass
        for spk, vec in emb.items():
            if not vec:
                continue
            v = np.asarray(vec, dtype=np.float64)
            n = np.linalg.norm(v)
            if not n:
                continue
            rows.append({"clip": clip, "spk": spk, "ts": ts, "v": v / n,
                         "sec": secs.get(spk, 0.0), "name": named.get(spk)})
    return rows


def sessionise(rows):
    rows.sort(key=lambda r: r["ts"])
    sid, last = 0, None
    for r in rows:
        if last is not None and r["ts"] - last > SESSION_GAP:
            sid += 1
        r["session"] = sid
        last = r["ts"]
    return sid + 1


def pct(a, qs=(5, 25, 50, 75, 95)):
    return "  ".join(f"p{q}={np.percentile(a, q):.3f}" for q in qs)


def main():
    rows = load()
    nsess = sessionise(rows)
    keep = [r for r in rows if r["sec"] >= MIN_SECONDS]
    print(f"  {len(rows)} voice embeddings, {len(keep)} with >= {MIN_SECONDS}s of speech")
    print(f"  {nsess} sessions (gap > {SESSION_GAP//60} min), "
          f"{(rows[-1]['ts']-rows[0]['ts'])/86400:.1f} days")
    if len(keep) < 50:
        print("  too few to cluster")
        return 1

    X = np.stack([r["v"] for r in keep])
    sess = np.array([r["session"] for r in keep])
    day = np.array([r["ts"] // 86400 for r in keep])

    S = np.clip(X @ X.T, -1.0, 1.0)          # cosine similarity
    D = 1.0 - S
    np.fill_diagonal(D, 0.0)
    D = (D + D.T) / 2.0

    iu = np.triu_indices(len(keep), 1)
    same_sess = sess[iu[0]] == sess[iu[1]]
    sims = S[iu]

    print("\n== Test A: does similarity track the recording, or the voice? ==")
    # Three buckets, because "same session" conflates two different things:
    # clips that are literally contiguous audio, and clips an hour apart in the
    # same sitting. If similarity collapses as soon as the clips stop being
    # adjacent, the embedding is tracking short-term acoustics, not identity.
    ts = np.array([r["ts"] for r in keep])
    dt = np.abs(ts[iu[0]] - ts[iu[1]])
    adj = same_sess & (dt <= 120)
    far = same_sess & (dt > 300)
    for lab, m in (("adjacent (<2 min apart)", adj),
                   ("same session, >5 min apart", far),
                   ("different session", ~same_sess)):
        if m.sum():
            a = sims[m]
            print(f"  {lab:<28} n={m.sum():>7}  {pct(a)}  p99={np.percentile(a,99):.3f}  max={a.max():.3f}")
    print("  (A personal wearable: one voice is in most sessions, so the same")
    print("   person SHOULD recur across them. If only the adjacent bucket")
    print("   scores high, the embedding is reading the recording, not the person.)")

    print("\n== Test B: do clusters recur across sessions, or sit inside one? ==")
    Z = linkage(squareform(D, checks=False), method="average")
    for r_ in (0.10, 0.15, 0.20, 0.25, 0.30):
        lab = fcluster(Z, t=r_, criterion="distance")
        sizes = np.bincount(lab)
        big = [c for c in range(1, len(sizes)) if sizes[c] >= 5]
        if not big:
            print(f"  radius {1-r_:.2f} cos: no cluster reaches 5 members")
            continue
        spans, dspans = [], []
        for c in big:
            m = lab == c
            spans.append(len(set(sess[m])))
            dspans.append(len(set(day[m])))
        multi = sum(1 for s in spans if s > 1)
        print(f"  radius {1-r_:.2f} cos: {len(set(lab))} clusters, "
              f"{len(big)} with >=5 members, "
              f"{multi}/{len(big)} span >1 session, "
              f"max sessions={max(spans)}, max days={max(dspans)}")
        if abs(r_ - 0.15) < 1e-9:
            order = sorted(big, key=lambda c: -sizes[c])[:8]
            print("     largest clusters at this radius:")
            for c in order:
                m = lab == c
                nm = sorted({r["name"] for r, k in zip(keep, m) if k and r["name"]})
                span = (ts[m].max() - ts[m].min()) / 60.0
                print(f"       n={sizes[c]:>3}  sessions={len(set(sess[m])):>2}  "
                      f"days={len(set(day[m])):>2}  span={span:>7.1f} min  "
                      f"named={','.join(nm) or '-'}")

    print("\n== Test C: the labelled voices, same session vs across sessions ==")
    if os.path.exists(SPK):
        meta = json.load(open(SPK))
        anchors = defaultdict(list)
        for name, info in meta.items():
            for s in (info.get("samples") or []):
                for r in keep:
                    if r["clip"] == s.get("clip") and r["spk"] == s.get("speaker"):
                        anchors[name].append(r)
        for name, rs in anchors.items():
            if len(rs) < 2:
                print(f"  {name}: {len(rs)} sample(s) with enough speech — cannot pair")
                continue
            same, cross = [], []
            for i in range(len(rs)):
                for j in range(i + 1, len(rs)):
                    s = float(rs[i]["v"] @ rs[j]["v"])
                    (same if rs[i]["session"] == rs[j]["session"] else cross).append(s)
            f = lambda a: (f"{np.mean(a):.3f} (n={len(a)})" if a else "none")
            span = (max(r["ts"] for r in rs) - min(r["ts"] for r in rs)) / 60.0
            print(f"  {name:<22} same-session {f(same):<16} across-session {f(cross):<16}"
                  f" enrolment spans {span:.0f} min, {len(set(r['session'] for r in rs))} session(s)")
    else:
        print("  no speakers.json")

    print("\n== Test D: what a threshold would have to separate ==")
    hi = sims[sims > 0.5]
    hist, edges = np.histogram(hi, bins=np.arange(0.5, 1.001, 0.025))
    for h, e in zip(hist, edges):
        bar = "#" * min(60, int(60 * h / max(hist.max(), 1)))
        print(f"  {e:.3f} {h:>7} {bar}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
