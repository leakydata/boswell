#!/usr/bin/env python3
"""Does a voice still match itself tomorrow?

The question the whole store is built on, and the one nothing here could answer
until enough labelling had been done. The design assumes drift: it keeps one
reference per condition instead of one average per person, on the theory that a
voice recorded in a different room on a different day lands somewhere else in
embedding space. That theory was never tested.

It is testable now. Named people have references spanning days, each tied to a
clip whose recording time is in its name, so same-person similarity can be
binned by how far apart the two recordings were.

Two populations are reported and the difference matters:

  ALL references     includes those acquired by naming a cluster. Clusters are
                     built by chaining similarity, so their members resemble
                     each other by construction. Circular, and biased upward.

  HAND-MADE only     origin manual or confirmed: a person listened and said who
                     it was. Not selected for similarity, so not circular --
                     but there are few of them, so read the medians and ignore
                     the tails.

If both show the same shape, the shape is real: the circular measurement can
only flatter, so decay visible in it is decay that exists.
"""
import collections
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))
import speaker_store as store          # noqa: E402

BINS = [("< 1 hour", 3600), ("1-6 hours", 6 * 3600), ("6-24 hours", 86400),
        ("1-3 days", 3 * 86400), ("> 3 days", float("inf"))]


def clip_time(name):
    m = re.search(r"(\d{9,})", name or "")
    return int(m.group(1)) if m else None


def collect():
    c = store._conn()
    try:
        rows = c.execute("""
            SELECT v.vec, v.clip, v.origin, p.name
            FROM voiceprints v JOIN people p ON p.id = v.person_id
            WHERE p.name IS NOT NULL
              AND (p.kind IS NULL OR p.kind != 'media')""").fetchall()
    finally:
        c.close()
    by = collections.defaultdict(list)
    for r in rows:
        t = clip_time(r["clip"])
        if t is not None:
            by[r["name"]].append((t, store._unpack(r["vec"]), r["origin"]))
    return by


def report(by, keep, label):
    pairs = []
    for refs in by.values():
        sel = [x for x in refs if keep is None or x[2] in keep]
        for i in range(len(sel)):
            for j in range(i + 1, len(sel)):
                pairs.append((abs(sel[i][0] - sel[j][0]),
                              float(store.unit(sel[i][1]) @ store.unit(sel[j][1]))))
    grouped = collections.defaultdict(list)
    for gap, sim in pairs:
        for name, limit in BINS:
            if gap < limit:
                grouped[name].append(sim)
                break
    print(f"\n--- same person, {label} ---")
    print(f"{'time apart':>12} {'pairs':>8} {'median':>8} {'p10':>8}")
    for name, _ in BINS:
        if not grouped[name]:
            continue
        a = np.array(grouped[name])
        print(f"{name:>12} {len(a):>8} {np.median(a):>8.3f} "
              f"{np.percentile(a, 10):>8.3f}")


if __name__ == "__main__":
    by = collect()
    print(f"{'person':<24} {'refs':>5} {'span':>9}")
    for n, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        ts = [x[0] for x in v]
        print(f"{n:<24} {len(v):>5} {(max(ts) - min(ts)) / 86400:>7.1f}d")
    report(by, None, "ALL references (cluster-selected, circular)")
    report(by, ("manual", "confirmed"), "HAND-MADE only (not circular)")
    print(f"\nfor comparison, measured on matched-condition turns:")
    print(f"  different people   median 0.107   p99 0.572")
    print(f"  current MATCH_HIGH {store.MATCH_HIGH}")
