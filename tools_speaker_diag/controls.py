#!/usr/bin/env python3
"""Two controls that don't use time as a proxy for anything.

The nearest-neighbour result showed similarity tracking temporal proximity.
That has an innocent explanation: nearby clips may simply contain the same
person, and distant clips different people, in which case the embeddings are
working exactly as intended. These two tests separate the explanations by
holding the recording conditions fixed instead of varying them.

  1. SAME CLIP, DIFFERENT SPEAKERS. Two people diarized inside one 30-second
     clip share the room, the microphone, the gain, the noise floor and the
     codec exactly. Only the person differs. If the embedding describes a
     person, these must score LOW. If it describes a recording, they score
     high -- and that is decisive on its own.

  2. WITHIN ONE SESSION, DECAY WITH GAP. All pairs drawn from a single
     sitting, so the room and the device are held constant. If similarity is
     still high at 20 minutes, then only the room mattered and the earlier
     result was about sessions. If it collapses within a sitting, whatever is
     changing runs on a timescale of minutes.
"""
import os, sys
from collections import defaultdict
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cluster_diag import load, sessionise, MIN_SECONDS

rows = load(); sessionise(rows)
keep = [r for r in rows if r["sec"] >= MIN_SECONDS]

print("== Control 1: two different speakers inside ONE clip ==")
byclip = defaultdict(list)
for r in keep:
    byclip[r["clip"]].append(r)
pairs = []
for clip, rs in byclip.items():
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            pairs.append(float(rs[i]["v"] @ rs[j]["v"]))
if pairs:
    a = np.array(pairs)
    print(f"  {len(a)} such pairs (identical room, mic, gain, codec; different person)")
    print("  " + "  ".join(f"p{q}={np.percentile(a,q):.3f}" for q in (5,25,50,75,95)))
    print(f"  mean {a.mean():.3f}   fraction over 0.85: {(a>=0.85).mean():.1%}")
else:
    print("  none -- no clip has two speakers with enough speech")

print("\n== Control 2: decay INSIDE a single session (room held constant) ==")
bysess = defaultdict(list)
for r in keep:
    bysess[r["session"]].append(r)
buckets = [("< 1 min",0,60),("1-2 min",60,120),("2-5 min",120,300),
           ("5-10 min",300,600),("10-30 min",600,1800),("30-120 min",1800,7200)]
acc = {b[0]: [] for b in buckets}
for sid, rs in bysess.items():
    for i in range(len(rs)):
        for j in range(i + 1, len(rs)):
            dt = abs(rs[i]["ts"] - rs[j]["ts"])
            s = float(rs[i]["v"] @ rs[j]["v"])
            for lab, lo, hi in buckets:
                if lo <= dt < hi:
                    acc[lab].append(s); break
print("  gap within one sitting      n      median     p90    >=0.85")
for lab, _, _ in buckets:
    a = np.array(acc[lab])
    if len(a) < 20:
        print(f"  {lab:<24} {len(a):>6}   (too few)"); continue
    print(f"  {lab:<24} {len(a):>6}   {np.median(a):.3f}   {np.percentile(a,90):.3f}   {(a>=0.85).mean():6.1%}")
