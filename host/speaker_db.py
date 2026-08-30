#!/usr/bin/env python3
"""Read and enrol against the speaker store, from a shell.

The web interface owns this data now and is a much better place to do the work:
it can play a voice on its own, show the closest named people with scores, and
undo a naming that attached three hundred references at once. This is for the
cases a browser is awkward for -- a script, a remote shell, a quick look.

It reads the same SQLite store the web side writes, rather than keeping one of
its own. It previously read and wrote data/speakers.npz directly, which quietly
stopped being authoritative when the store moved, so `identify` was scoring
against centroids nothing else had used in some time and reporting the answer
with a straight face.

    uv run host/speaker_db.py list
    uv run host/speaker_db.py identify data/spk_other.npz
    uv run host/speaker_db.py enroll alice data/spk_meeting.npz SPEAKER_00

Enrolling adds one reference. It does not average anything into a centroid and
does not refuse a sample for being unlike what is already stored -- a voice in
a different room is meant to look different, and that sample is the valuable
one.
"""

import argparse
import json
import os
import sys

import numpy as np

DB_PATH = "data/speakers.npz"
META_PATH = "data/speakers.json"

# Below this, call it unknown rather than guess. Chosen from measured data:
# same speaker across recordings scored 0.65-0.87, different speakers 0.38-0.48.
MATCH_THRESHOLD = 0.60


def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def _store():
    """The live store. This tool no longer keeps one of its own.

    It used to read and write data/speakers.npz directly, which stopped being
    authoritative when the web side moved to SQLite -- so `identify` was
    scoring against centroids nothing else had used for some time and
    reporting the answer with a straight face. A stale second opinion is worse
    than no second opinion.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "web"))
    import speaker_store
    speaker_store.migrate()
    return speaker_store


def load_db():
    if not os.path.exists(DB_PATH):
        return {}, {}
    d = np.load(DB_PATH)
    meta = json.load(open(META_PATH)) if os.path.exists(META_PATH) else {}
    return {k: d[k] for k in d.files}, meta


def _web_store_in_use(meta):
    """True when the web interface owns this store.

    The web version keeps every enrolled recording as its own removable
    sample, with the centroid derived from them; this tool predates that and
    knows only about centroids and a count. Its save would rewrite
    speakers.json as {name: {count, sources}} and delete the samples lists --
    at the time of writing that is six recordings for one person and two for
    another, and a voiceprint of a conversation that already happened cannot
    be made again.
    """
    return any(isinstance(v, dict) and v.get("samples")
               for v in (meta or {}).values())


def save_db(vecs, meta):
    if _web_store_in_use(load_db()[1]):
        raise SystemExit(
            "data/speakers.json is the web interface's sample-based store, and "
            "writing it from here would discard the per-recording samples it "
            "holds. Enrol and remove voices in the web interface instead.")
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    # Same care as the web store: a truncated .npz cannot be regenerated.
    import io
    buf = io.BytesIO()
    np.savez(buf, **vecs)
    tmp = DB_PATH + ".part"
    with open(tmp, "wb") as f:
        f.write(buf.getvalue())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, DB_PATH)
    tmp = META_PATH + ".part"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, META_PATH)


def cmd_enroll(args):
    src = np.load(args.npz)
    if args.speaker not in src.files:
        print(f"{args.speaker} not in {args.npz}; has {src.files}", file=sys.stderr)
        return 1

    store = _store()
    c = store._conn()
    try:
        pid = store.person_id_for(args.name, c)
        # No averaging, and no resemblance check. Every enrolment is its own
        # reference: a sample unlike the ones already held covers a condition
        # they do not, which is the point.
        r = store.add_voiceprint(pid, src[args.speaker], clip=args.npz,
                                 speaker=args.speaker, origin="manual", c=c)
        if not r.get("ok"):
            print(f"refused: {r.get('detail', r.get('reason'))}", file=sys.stderr)
            return 1
        n = len(store.voiceprints(pid, c))
    finally:
        c.close()
    print(f"enrolled '{args.name}' ({n} reference{'' if n == 1 else 's'})")
    return 0


def cmd_list(args):
    store = _store()
    people = [p for p in store.people() if p["name"]]
    if not people:
        print("no speakers enrolled")
        return 0
    print(f"{len(people)} enrolled:")
    for p in people:
        kind = f"  [{p['kind']}]" if p.get("kind") else ""
        print(f"  {p['name']:<24} {p['prints']} reference(s), "
              f"{p['seconds']:.0f}s{kind}")
    return 0


def cmd_identify(args):
    store = _store()
    src = np.load(args.npz)
    c = store._conn()
    try:
        for spk in src.files:
            r = store.match(src[spk], c)
            who = r.get("name") or "UNKNOWN"
            cands = ", ".join(f"{x['name']} {x['score']:.3f}"
                              for x in r.get("candidates", [])[:3]
                              if x.get("name"))
            print(f"  {spk:<14} {who:<20} {r['decision']:<10} "
                  f"score {r['score']:.3f}" + (f"   [{cands}]" if cands else ""))
    finally:
        c.close()
    return 0


def _cmd_identify_legacy(args):
    vecs, meta = load_db()
    if not vecs:
        print("no speakers enrolled yet", file=sys.stderr)
        return 1

    src = np.load(args.npz)
    print(f"{args.npz}\n")
    for key in src.files:
        v = unit(src[key])
        scores = sorted(((float(v @ unit(vecs[n])), n) for n in vecs), reverse=True)
        best, name = scores[0]
        if best >= MATCH_THRESHOLD:
            verdict = f"-> {name}   ({best:.3f})"
        else:
            verdict = f"-> UNKNOWN  (best {name} {best:.3f}, below {MATCH_THRESHOLD})"
        print(f"  {key:<12} {verdict}")
        for s, n in scores[1:3]:
            print(f"  {'':<12}    runner-up {n}: {s:.3f}")
    return 0


def cmd_label(args):
    """Interactive: name each speaker in a file and enroll in one pass."""
    vecs, meta = load_db()
    src = np.load(args.npz)
    for key in src.files:
        v = unit(src[key])
        hint = ""
        if vecs:
            best, name = max(((float(v @ unit(vecs[n])), n) for n in vecs))
            if best >= MATCH_THRESHOLD:
                hint = f" [looks like {name}, {best:.3f}]"
        ans = input(f"  name for {key}{hint} (blank to skip): ").strip()
        if not ans:
            continue
        a = argparse.Namespace(name=ans, npz=args.npz, speaker=key)
        cmd_enroll(a)
        vecs, meta = load_db()
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll"); e.add_argument("name")
    e.add_argument("npz"); e.add_argument("speaker"); e.set_defaults(fn=cmd_enroll)

    l = sub.add_parser("list"); l.set_defaults(fn=cmd_list)

    i = sub.add_parser("identify"); i.add_argument("npz"); i.set_defaults(fn=cmd_identify)

    b = sub.add_parser("label"); b.add_argument("npz"); b.set_defaults(fn=cmd_label)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
