#!/usr/bin/env python3
"""For each voice embedding, how far away in TIME is its nearest neighbour?

The session comparison is confounded: a session contains several speakers, so
pairs drawn from it are mostly different people and score low for the right
reason. This test is not confounded. Each vector is asked one question -- of
all 883 other voices in the archive, which is most like you? -- and the answer
is scored only by how long ago it was recorded.

Chance is overwhelmingly against adjacency. Only 0.6% of all pairs are within
two minutes of each other; 80% are in a different session entirely. So if the
embedding describes a PERSON, nearest neighbours should be scattered across
days, because the wearer is in most sessions. If it describes a RECORDING,
they will pile up in the minutes either side.
"""
import os, sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cluster_diag import load, sessionise, MIN_SECONDS

rows = load(); nsess = sessionise(rows)
keep = [r for r in rows if r["sec"] >= MIN_SECONDS]
X = np.stack([r["v"] for r in keep]); ts = np.array([r["ts"] for r in keep])
sess = np.array([r["session"] for r in keep])
S = np.clip(X @ X.T, -1.0, 1.0); np.fill_diagonal(S, -2.0)

nn = S.argmax(1); best = S.max(1)
gap = np.abs(ts - ts[nn]); cross = sess != sess[nn]

n = len(keep)
# What adjacency would look like by chance, given the archive's own shape.
iu = np.triu_indices(n, 1)
alldt = np.abs(ts[iu[0]] - ts[iu[1]])
print(f"  {n} voices, {nsess} sessions, {(ts.max()-ts.min())/86400:.1f} days\n")
print("  time gap to nearest neighbour        observed      by chance")
for lab, lo, hi in (("< 1 min", 0, 60), ("1-2 min", 60, 120), ("2-10 min", 120, 600),
                    ("10-60 min", 600, 3600), ("1-6 hours", 3600, 21600),
                    ("> 6 hours", 21600, 10**9)):
    o = ((gap >= lo) & (gap < hi)).mean()
    c = ((alldt >= lo) & (alldt < hi)).mean()
    print(f"  {lab:<34} {o:6.1%}   {c:11.1%}")
print(f"\n  nearest neighbour in a DIFFERENT session: {cross.mean():6.1%}"
      f"   (by chance {(sess[iu[0]]!=sess[iu[1]]).mean():.1%})")
print(f"  median similarity to nearest neighbour:   {np.median(best):.3f}")

# The same question restricted to strong matches only -- the ones a threshold
# would actually name.
for t in (0.75, 0.85):
    m = best >= t
    if m.sum():
        print(f"\n  of the {m.sum()} voices whose best match clears {t}:")
        print(f"    within 2 min:        {(gap[m] <= 120).mean():6.1%}")
        print(f"    different session:   {cross[m].mean():6.1%}")
        print(f"    median gap:          {np.median(gap[m])/60:.1f} min")
