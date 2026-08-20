#!/usr/bin/env python3
"""
Named-speaker enrollment database.

Diarization only ever produces per-file labels (SPEAKER_00, SPEAKER_01) that
mean nothing across recordings. This maps those to real people by cosine
matching against enrolled voiceprints.

There is no training step. Enrolling appends to a running centroid, so a
person's print sharpens with every label you add and is usable from the first.

Usage:
    uv run host/speaker_db.py enroll  alice   data/spk_vad_sample.npz SPEAKER_00
    uv run host/speaker_db.py list
    uv run host/speaker_db.py identify data/spk_ble_voice.npz
    uv run host/speaker_db.py label    data/spk_vad_sample.npz
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

    vec = unit(src[args.speaker])
    vecs, meta = load_db()

    if args.name in vecs:
        n = meta.get(args.name, {}).get("count", 1)
        # Running mean in embedding space, renormalised so the centroid stays
        # comparable by cosine.
        vecs[args.name] = unit(unit(vecs[args.name]) * n + vec)
        meta[args.name]["count"] = n + 1
        print(f"updated '{args.name}' (now {n + 1} samples)")
    else:
        vecs[args.name] = vec
        meta[args.name] = {"count": 1}
        print(f"enrolled '{args.name}' (1 sample)")

    meta[args.name].setdefault("sources", []).append(f"{args.npz}:{args.speaker}")
    save_db(vecs, meta)
    return 0


def cmd_list(args):
    vecs, meta = load_db()
    if not vecs:
        print("no speakers enrolled")
        return 0
    print(f"{len(vecs)} enrolled:")
    for name in sorted(vecs):
        c = meta.get(name, {}).get("count", "?")
        print(f"  {name:<20} {c} sample(s)")
    if len(vecs) > 1:
        print("\n  pairwise similarity between enrolled people:")
        names = sorted(vecs)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                s = float(unit(vecs[a]) @ unit(vecs[b]))
                warn = "   <-- too close, may confuse" if s > MATCH_THRESHOLD else ""
                print(f"    {a} vs {b}: {s:.3f}{warn}")
    return 0


def cmd_identify(args):
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
