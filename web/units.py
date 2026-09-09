"""A search index over things people said, rather than over transcript lines.

The archive embeds one vector per transcript line, and a line is whatever fell
inside a thirty-second clip. Measured over this archive: **median seven words,
and 61% of lines under ten**. No embedding model recovers much from "but it
has a subscription" -- the meaning was never in the fragment, it was in the
sentence the clip boundary cut in half.

Stitched units are the same speech with the boundary undone: median eighteen
words, p90 of 107, and the share under ten words falls from 61% to 36%. That
is a bigger change to what the model sees than any encoder swap, and it was
worth measuring before reaching for a bigger model -- the prefix change that
looked obviously right on the model card made retrieval slightly *worse* here
when it was actually tested.

**Rebuilt whole, never patched.** Units are derived: they move when a
transcript is edited, a name is applied, or a clip is re-transcribed. Keeping
a derived index correct by invalidation is the machinery that produced this
project's worst bugs -- a storage figure twenty-one hours stale beside a
battery reading a minute old, a status file that said "syncing" for three
hours while every attempt failed. A full rebuild takes about a hundred
seconds for the whole archive, so there is no reason to carry that risk.

The rebuild replaces every row inside one transaction rather than building a
second table and renaming it. Renaming looked cleaner and does not work: a
sqlite-vec `vec0` table owns shadow tables (`..._chunks`, `..._rowids`), and
ALTER TABLE RENAME moves the virtual table without them, leaving an index
that reports `no such table: main.unit_vec_chunks` on the first search. In
WAL a reader holds its snapshot until the commit lands, which is the same
atomicity the rename was reaching for and is actually true.

This sits beside the line index rather than replacing it: `seg` still answers
exact quotes and keyword matching, `unit` answers "what was that about".
"""
import json
import os
import sqlite3
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
DB = os.path.join(DATA, "semantic.db")

# Sound tags too unreliable to be searched on.
#
# `Speech` sits on 93% of clips and separates nothing. The transport family is
# worse than useless: 450 clips carry `Vehicle`, 364 of them between midnight
# and 5am and 248 of those with no speech at all -- a microphone beside a fan,
# not a car. The tell is that `Mechanical fan` and `Air conditioning` never
# co-occur with it: the tagger picks `Vehicle` *instead* of the fan, and
# reaches for its neighbours `Train`, `Boat` and `Rail transport` too.
UNTRUSTED_SOUNDS = {
    "Speech", "Vehicle", "Car", "Train", "Boat, Water vehicle",
    "Railroad car, train wagon", "Rail transport",
}


def _connect():
    import semantic
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    # So a rebuild's long write transaction does not block readers, and so
    # they keep seeing the previous index until it commits.
    db.execute("PRAGMA journal_mode=WAL")
    return db, semantic._vec_available(db)


def _schema(db, have_vec, table="unit"):
    import semantic
    db.execute(f"""CREATE TABLE IF NOT EXISTS {table}(
        id INTEGER PRIMARY KEY,
        clip TEXT, idx INTEGER,          -- first line, the handle back to seg
        conv TEXT,                       -- the conversation it belongs to
        section INTEGER,                 -- which run of subject within it
        at REAL, until REAL, time_known INTEGER,
        speaker TEXT, name TEXT, device_id TEXT,
        clips TEXT,                      -- JSON: every clip it draws on
        sounds TEXT,                     -- JSON: situational tags, curated
        lines INTEGER, words INTEGER,
        text TEXT, vec BLOB,
        UNIQUE(clip, idx))""")
    if have_vec:
        # Cosine like the others: these are compared by direction, and L2
        # reported distances past 2 that ranked negative.
        db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS {table}_vec
                       USING vec0(embedding float[{semantic.DIM}]
                                  distance_metric=cosine)""")


def _sounds_for(clips, index_db):
    """The situation a unit happened in, from the clips it draws on.

    Tags are per clip and a unit can span two, so this is the union -- which
    is right, because the situation genuinely spans the boundary. Only the
    confident set is read: the low-bar column carries `Vehicle` on 624 clips
    of which four clear 0.35, and a filter built on that excludes silently.
    """
    out = []
    for name in clips:
        row = index_db.clip_row(name) if hasattr(index_db, "clip_row") else None
        strong = (row or {}).get("sounds_strong") if row else None
        if strong is None:
            r = index_db._conn().execute(
                "SELECT sounds_strong FROM clips WHERE name=?", (name,)).fetchone()
            strong = r["sounds_strong"] if r else None
        for tag in (strong or "").split("\n"):
            tag = tag.strip()
            if tag and tag not in UNTRUSTED_SOUNDS and tag not in out:
                out.append(tag)
    return out


def rebuild(limit=5000, on_progress=None):
    """Rebuild the whole unit index from the transcripts, then swap it in.

    Returns how many units were written. Slow enough to want a progress
    callback and fast enough that nothing incremental is worth building.
    """
    import index_db
    import semantic
    import threads

    db, have_vec = _connect()
    _schema(db, have_vec, "unit")
    db.execute("BEGIN")
    db.execute("DELETE FROM unit")
    if have_vec:
        db.execute("DELETE FROM unit_vec")

    written = 0
    convs = index_db.conversations(limit=limit)
    for n, g in enumerate(convs):
        try:
            built = threads.for_conversation(g["clips"])
        except Exception:
            continue                     # one bad conversation is not a failure
        # Which section each unit fell in, so a search can stay inside a
        # subject rather than wandering out of it.
        where = {}
        for si, sec in enumerate(built.get("sections") or []):
            for u in sec["units"]:
                where[id(u)] = si
        conv = g["clips"][0] if g.get("clips") else None
        for u in built["units"]:
            text = (u.get("text") or "").strip()
            if len(text) < semantic.MIN_CHARS:
                continue
            try:
                vec = semantic.embed(text)
            except Exception:
                continue                 # the embedder being down is not a bug here
            clips = sorted({p.get("clip") for p in u.get("parts", []) if p.get("clip")})
            cur = db.execute(
                """INSERT OR IGNORE INTO unit
                   (clip, idx, conv, section, at, until, time_known,
                    speaker, name, device_id, clips, sounds, lines, words,
                    text, vec)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (u.get("clip"), u.get("index"), conv, where.get(id(u)),
                 u.get("at"), u.get("until"),
                 1 if u.get("at") is not None else 0,
                 u.get("speaker"), u.get("name"), u.get("device_id"),
                 json.dumps(clips), json.dumps(_sounds_for(clips, index_db)),
                 u.get("lines"), u.get("words"), text, semantic._pack(vec)))
            if cur.rowcount and have_vec:
                db.execute("INSERT INTO unit_vec(rowid, embedding) VALUES (?, ?)",
                           (cur.lastrowid, semantic._pack(vec)))
            written += cur.rowcount
        if on_progress:
            on_progress(n + 1, len(convs), written)
    # One commit. Until it lands, every reader still sees the previous
    # index in full; after it, the new one in full. Never half of either.
    db.commit()
    db.close()
    return written


def search(query, limit=25, device=None, name=None, sound=None,
           since=None, until=None):
    """Units closest in meaning, newest-relevant first, with filters.

    The filters are the point of storing metadata beside the vector: "what did
    Blase say about the roof, while I was at the desk, last week" is three
    different kinds of question and only one of them is semantic.
    """
    import semantic
    db, have_vec = _connect()
    try:
        try:
            qv = semantic.embed(query)
        except Exception as e:
            return {"hits": [], "error": f"embedder unavailable: {e}"}

        rows = []
        if have_vec:
            # Over-fetch, because filtering after the vector search can empty
            # a page that had matches further down.
            got = db.execute(
                """SELECT rowid, distance FROM unit_vec
                   WHERE embedding MATCH ? AND k = ?""",
                (semantic._pack(qv), max(limit * 8, 100))).fetchall()
            order = {r["rowid"]: (i, r["distance"]) for i, r in enumerate(got)}
            if order:
                qs = ",".join("?" * len(order))
                for r in db.execute(f"SELECT * FROM unit WHERE id IN ({qs})",
                                    list(order)):
                    rows.append((order[r["id"]][1], r))
                rows.sort(key=lambda t: t[0])
        else:
            for r in db.execute("SELECT * FROM unit"):
                if not r["vec"]:
                    continue
                v = semantic._unpack(r["vec"])
                dot = sum(a * b for a, b in zip(qv, v))
                rows.append((1.0 - dot, r))
            rows.sort(key=lambda t: t[0])

        hits = []
        for dist, r in rows:
            if device and r["device_id"] != device:
                continue
            if name and (r["name"] or "") != name:
                continue
            if sound:
                try:
                    tags = json.loads(r["sounds"] or "[]")
                except ValueError:
                    tags = []
                if sound not in tags:
                    continue
            if since and (r["at"] or 0) < since:
                continue
            if until and (r["at"] or 0) > until:
                continue
            hits.append({
                "clip": r["clip"], "index": r["idx"], "conv": r["conv"],
                "section": r["section"], "at": r["at"], "until": r["until"],
                "time_known": bool(r["time_known"]),
                "name": r["name"], "speaker": r["speaker"],
                "device_id": r["device_id"],
                "clips": json.loads(r["clips"] or "[]"),
                "sounds": json.loads(r["sounds"] or "[]"),
                "lines": r["lines"], "words": r["words"],
                "text": r["text"],
                "score": round(1.0 - dist, 4),
            })
            if len(hits) >= limit:
                break
        return {"hits": hits}
    finally:
        db.close()


def stats():
    """What is in the index, and how old it is."""
    db, _ = _connect()
    try:
        have = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unit'").fetchone()
        if not have:
            return {"units": 0, "built": None}
        r = db.execute("SELECT COUNT(*) n, MAX(until) newest FROM unit").fetchone()
        return {"units": r["n"], "newest": r["newest"]}
    finally:
        db.close()


def main():
    import argparse
    import sys
    sys.path.insert(0, HERE)
    ap = argparse.ArgumentParser(description="Rebuild the unit search index.")
    ap.add_argument("--limit", type=int, default=5000,
                    help="how many clips back to group into conversations")
    ap.add_argument("--search", help="search instead of rebuilding")
    args = ap.parse_args()

    if args.search:
        for h in search(args.search, limit=8)["hits"]:
            when = time.strftime("%m-%d %H:%M", time.localtime(h["at"])) if h["at"] else "?"
            who = h["name"] or h["speaker"] or "?"
            print(f"  {h['score']:.3f}  {when}  {who:<14} {h['text'][:80]}")
        return

    t0 = time.time()
    n = rebuild(limit=args.limit,
                on_progress=lambda i, total, w: print(
                    f"\r  {i}/{total} conversations, {w} units", end="", flush=True))
    print(f"\n{n} units in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
