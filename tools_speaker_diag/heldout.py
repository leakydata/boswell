#!/usr/bin/env python3
"""How often is a name right? Measured, not estimated.

Every number this project has produced so far is a distribution: how alike two
turns of one voice are, how far apart two people sit. None of them says what a
user cares about, which is whether the name on the screen is the right name.

The only way to know is to hide some of the labels and see whether the system
recovers them. Hold out one hand-made reference at a time, match its audio
against everything else, and compare the answer to what a person said it was.
That is precision and recall, and it is not circular: the held-out reference
took no part in the decision.

Two rules keep it honest, and both cost accuracy:

  Only hand-made references are held out. References acquired by naming a
  cluster were grouped by similarity in the first place, so recovering one
  proves the clustering was self-consistent and nothing more.

  Every reference from the same CLIP as the held-out one is removed too. A
  voiceprint and its neighbour from the same recording are near-duplicates;
  leaving them in measures whether the system can find a copy of a thing, which
  it always can, rather than whether it can recognise a person.

It refuses to report below a floor of held-out cases. A precision figure from
fifteen labels would look like evidence without being any, which is the exact
mistake this project already made once with a different-speaker figure drawn
from three pairs.

    uv run tools_speaker_diag/heldout.py [--min-cases 40]
"""
import argparse
import collections
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "web"))

import speaker_store as store         # noqa: E402

HAND = ("manual", "confirmed")


def load():
    c = store._conn()
    try:
        rows = c.execute("""
            SELECT v.id, v.person_id, v.vec, v.clip, v.origin, p.name, p.kind
            FROM voiceprints v JOIN people p ON p.id = v.person_id
            WHERE p.name IS NOT NULL
              AND (p.kind IS NULL OR p.kind != 'media')""").fetchall()
    finally:
        c.close()
    return [{"id": r["id"], "person": r["name"], "clip": r["clip"],
             "origin": r["origin"], "vec": store.unit(store._unpack(r["vec"]))}
            for r in rows]


def evaluate(refs):
    """Leave-one-out over the hand-made references."""
    cases = []
    for held in refs:
        if held["origin"] not in HAND:
            continue
        pool = [r for r in refs
                if r["id"] != held["id"]
                and not (held["clip"] and r["clip"] == held["clip"])]
        if not pool:
            continue
        best = {}
        for r in pool:
            s = float(held["vec"] @ r["vec"])
            if r["person"] not in best or s > best[r["person"]]:
                best[r["person"]] = s
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        top, score = ranked[0]
        margin = score - ranked[1][1] if len(ranked) > 1 else None

        if margin is None:
            decided = score >= store.MATCH_HIGH
        else:
            decided = score >= store.MATCH_HIGH and margin >= store.MARGIN_MIN
        cases.append({"truth": held["person"], "guess": top if decided else None,
                      "score": score, "margin": margin,
                      "would_be_right": top == held["person"]})
    return cases


def report(cases):
    named = [c for c in cases if c["guess"]]
    correct = [c for c in named if c["guess"] == c["truth"]]
    recoverable = [c for c in cases if c["would_be_right"]]

    print(f"held-out cases            : {len(cases)}")
    print(f"the system put a name to  : {len(named)}")
    if named:
        print(f"  of those, correct       : {len(correct)}  "
              f"({100 * len(correct) / len(named):.0f}% precision)")
    print(f"  left unnamed            : {len(cases) - len(named)}")
    print(f"recall (named AND correct): {100 * len(correct) / len(cases):.0f}%")
    print(f"\nthe right person was top-ranked in {len(recoverable)} of "
          f"{len(cases)} cases ({100 * len(recoverable) / len(cases):.0f}%)")
    print("-- the gap between that and recall is what the thresholds are "
          "refusing.")

    wrong = [c for c in named if c["guess"] != c["truth"]]
    if wrong:
        print(f"\n{len(wrong)} wrong name(s) — the expensive errors:")
        for c in sorted(wrong, key=lambda c: -c["score"])[:8]:
            m = "n/a" if c["margin"] is None else f"{c['margin']:.3f}"
            print(f"   said {c['guess']!r} for {c['truth']!r} "
                  f"(score {c['score']:.3f}, margin {m})")

    missed = [c for c in cases if not c["guess"] and c["would_be_right"]]
    if missed:
        print(f"\n{len(missed)} case(s) the thresholds rejected but would have "
              f"been right:")
        for c in sorted(missed, key=lambda c: -c["score"])[:8]:
            m = "n/a" if c["margin"] is None else f"{c['margin']:.3f}"
            print(f"   {c['truth']!r} at score {c['score']:.3f}, margin {m}")
        best = max(c["score"] for c in missed)
        print(f"   the highest of them scores {best:.3f}, against "
              f"MATCH_HIGH {store.MATCH_HIGH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-cases", type=int, default=40,
                    help="refuse to report below this many held-out cases")
    a = ap.parse_args()

    refs = load()
    by = collections.Counter(r["person"] for r in refs if r["origin"] in HAND)
    hand = sum(by.values())

    print(f"{hand} hand-made reference(s) across {len(by)} "
          f"{'person' if len(by) == 1 else 'people'}:")
    for n, k in by.most_common():
        print(f"   {n}: {k}")

    if hand < a.min_cases:
        print(f"\nNot enough to measure yet: {hand} of {a.min_cases}.")
        print("A precision figure from this many would look like evidence "
              "without being\nany, and this project has already made that "
              "mistake once.\n")
        print("Hand-made references come from naming a voice in the "
              "interface. The\nfastest way to accumulate them is the "
              "'Since yesterday' queue: identity has\nto be re-earned daily "
              "anyway, so a couple of minutes a day both keeps the\narchive "
              "labelled and builds the evidence this needs.")
        return

    print()
    report(evaluate(refs))


if __name__ == "__main__":
    main()
