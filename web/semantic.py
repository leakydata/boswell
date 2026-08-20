"""
Semantic search over transcript segments.

Keyword search finds a conversation only if you remember a word from it.
"the bit about the battery connector" finds nothing unless someone said
"connector". Embedding each line and searching by meaning finds it anyway.

This is deliberately not used for speaker matching. Voiceprints are compared
against one reference vector per enrolled person -- four of them here -- so
that search is already trivial and an index would only add moving parts.
What is worth indexing is the transcript, which grows without limit.

Embeddings come from Ollama's nomic-embed-text, which is already pulled for
this machine and runs locally like everything else here.
"""

import json
import os
import sqlite3
import struct

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
DB = os.path.join(DATA, "semantic.db")

OLLAMA_EMBED = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"
DIM = 768

# Lines shorter than this are "Yeah", "Right", "Okay" -- they embed to noise
# and crowd out real matches.
MIN_CHARS = 25


def _vec_available(db):
    try:
        import sqlite_vec
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        return True
    except Exception:
        return False


def _connect():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    have_vec = _vec_available(db)
    db.execute("""CREATE TABLE IF NOT EXISTS seg(
        id INTEGER PRIMARY KEY,
        clip TEXT, idx INTEGER, start REAL, text TEXT, speaker TEXT,
        vec BLOB,
        UNIQUE(clip, idx))""")
    # What the agent has already recorded, embedded alongside the transcripts.
    #
    # Without this the agent reviewed every conversation from zero: it was
    # handed a system prompt and a transcript and nothing else, so it could
    # not know it had already written down the same thing. The result is
    # visible in the store -- "Nathan has a shoulder impingement" and "Nathan
    # had a cortisone injection 2 weeks ago for shoulder impingement" as two
    # unrelated facts, the same person filed under both "Nathan" and "Owner",
    # and one goal recorded twice in different words.
    db.execute("""CREATE TABLE IF NOT EXISTS mem(
        id INTEGER PRIMARY KEY,
        item_id TEXT UNIQUE, kind TEXT, subject TEXT, text TEXT,
        recorded_at TEXT, vec BLOB)""")
    if have_vec:
        # Cosine, not the default L2. These embeddings are compared by
        # direction, and with L2 the reported distance ran past 2 so the
        # score came out negative and could not be ranked or shown.
        db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS seg_vec
                       USING vec0(embedding float[{DIM}] distance_metric=cosine)""")
        db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS mem_vec
                       USING vec0(embedding float[{DIM}] distance_metric=cosine)""")
    db.commit()
    return db, have_vec


def embed(text):
    r = requests.post(OLLAMA_EMBED,
                      json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
    r.raise_for_status()
    v = r.json().get("embedding") or []
    if len(v) != DIM:
        raise ValueError(f"expected {DIM} dims, got {len(v)}")
    return v


def _pack(v):
    return struct.pack(f"{len(v)}f", *v)


def _unpack(b):
    return list(struct.unpack(f"{len(b)//4}f", b))


def index_clip(name, segments, replace=False):
    """Embed and store one clip's lines. Existing rows are left alone unless
    `replace`, so re-indexing the whole archive is cheap and resumable."""
    db, have_vec = _connect()
    added = 0
    failed = 0
    first_error = None
    try:
        if replace:
            # Clear the clip out first rather than upserting over it.
            #
            # Upserting only touches the indices the new transcript happens to
            # have. Editing a transcript shorter, or splitting a clip, left the
            # rows for the removed lines in place -- so semantic search kept
            # returning text that no longer exists in the transcript, with a
            # timestamp pointing at audio that says something else.
            for r in db.execute("SELECT id FROM seg WHERE clip=?",
                                (name,)).fetchall():
                if have_vec:
                    db.execute("DELETE FROM seg_vec WHERE rowid=?", (r["id"],))
            db.execute("DELETE FROM seg WHERE clip=?", (name,))

        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if len(text) < MIN_CHARS:
                continue
            if not replace:
                row = db.execute("SELECT 1 FROM seg WHERE clip=? AND idx=?",
                                 (name, i)).fetchone()
                if row:
                    continue
            try:
                v = embed(text)
            except Exception as e:
                # Counted, not swallowed. Ollama being down produced an index
                # that was quietly missing lines, and search that returned
                # nothing looked like an absence of matches rather than an
                # absence of data.
                failed += 1
                if first_error is None:
                    first_error = f"{type(e).__name__}: {e}"[:120]
                continue
            db.execute(
                """INSERT INTO seg(clip, idx, start, text, speaker, vec)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(clip, idx) DO UPDATE SET
                     text=excluded.text, speaker=excluded.speaker,
                     start=excluded.start, vec=excluded.vec""",
                (name, i, seg.get("start", 0.0), text, seg.get("speaker"),
                 _pack(v)))
            if have_vec:
                # Selected, not taken from lastrowid: on a conflict that took
                # the UPDATE branch, lastrowid is not the row that was updated.
                rid = db.execute(
                    "SELECT id FROM seg WHERE clip=? AND idx=?",
                    (name, i)).fetchone()["id"]
                db.execute("DELETE FROM seg_vec WHERE rowid=?", (rid,))
                db.execute("INSERT INTO seg_vec(rowid, embedding) VALUES(?,?)",
                           (rid, _pack(v)))
            added += 1
        db.commit()
    finally:
        db.close()
    return {"added": added, "failed": failed, "error": first_error}


def item_text(item):
    """One line describing an agent item, for embedding and for recall.

    The kind and subject are part of the text on purpose: "Nathan: has a
    shoulder impingement" should sit near a later conversation about his
    shoulder, and a bare fact body often does not carry who it is about.
    """
    kind = (item.get("_kind") or "").rstrip("s")
    subject = item.get("subject") or item.get("owner") or ""
    body = (item.get("fact") or item.get("title") or item.get("text")
            or item.get("body") or "")
    extra = item.get("body") if item.get("title") else ""
    parts = [p for p in (kind, subject, body, extra) if p]
    return " · ".join(parts)[:1000]


def index_item(item):
    """Embed one agent item so later reviews can recall it."""
    text = item_text(item)
    iid = item.get("_id")
    if not text or not iid:
        return False
    db, have_vec = _connect()
    try:
        v = embed(text)
        db.execute("""INSERT INTO mem(item_id, kind, subject, text, recorded_at, vec)
                      VALUES(?,?,?,?,?,?)
                      ON CONFLICT(item_id) DO UPDATE SET
                        kind=excluded.kind, subject=excluded.subject,
                        text=excluded.text, recorded_at=excluded.recorded_at,
                        vec=excluded.vec""",
                   (iid, item.get("_kind"), item.get("subject"), text,
                    item.get("_recorded_at"), _pack(v)))
        if have_vec:
            rid = db.execute("SELECT id FROM mem WHERE item_id=?",
                             (iid,)).fetchone()["id"]
            db.execute("DELETE FROM mem_vec WHERE rowid=?", (rid,))
            db.execute("INSERT INTO mem_vec(rowid, embedding) VALUES(?,?)",
                       (rid, _pack(v)))
        db.commit()
        return True
    finally:
        db.close()


def remove_item(item_id):
    db, have_vec = _connect()
    try:
        row = db.execute("SELECT id FROM mem WHERE item_id=?", (item_id,)).fetchone()
        if row and have_vec:
            db.execute("DELETE FROM mem_vec WHERE rowid=?", (row["id"],))
        db.execute("DELETE FROM mem WHERE item_id=?", (item_id,))
        db.commit()
        return bool(row)
    finally:
        db.close()


def recall(query, limit=12):
    """Things already recorded that bear on this text.

    Used to give the agent what it already knows before it reviews a new
    conversation, so it can add to a picture instead of starting one.
    """
    db, have_vec = _connect()
    try:
        try:
            qv = embed(query)
        except Exception as e:
            return {"error": f"could not embed: {e}", "hits": []}
        if have_vec:
            rows = db.execute(
                """SELECT m.item_id, m.kind, m.subject, m.text, m.recorded_at,
                          v.distance
                   FROM mem_vec v JOIN mem m ON m.id = v.rowid
                   WHERE v.embedding MATCH ? AND k = ?
                   ORDER BY v.distance""",
                (_pack(qv), limit)).fetchall()
            hits = [{"id": r["item_id"], "kind": r["kind"],
                     "subject": r["subject"], "text": r["text"],
                     "recorded_at": r["recorded_at"],
                     "score": round(1.0 - r["distance"], 3)} for r in rows]
        else:
            hits = _brute_force(db, qv, "mem", limit)
        return {"hits": hits}
    finally:
        db.close()


def _brute_force(db, qv, table, limit):
    """Cosine over stored vectors, for when the extension is unavailable."""
    import math
    qn = math.sqrt(sum(x * x for x in qv)) or 1.0
    out = []
    for r in db.execute(f"SELECT * FROM {table}"):
        v = _unpack(r["vec"])
        dot = sum(a * b for a, b in zip(qv, v))
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append((dot / (qn * n), r))
    out.sort(key=lambda t: -t[0])
    return [{"id": r["item_id"], "kind": r["kind"], "subject": r["subject"],
             "text": r["text"], "recorded_at": r["recorded_at"],
             "score": round(sc, 3)} for sc, r in out[:limit]]


def search(query, limit=25):
    """Lines closest in meaning to the query, best first."""
    db, have_vec = _connect()
    try:
        try:
            qv = embed(query)
        except Exception as e:
            return {"error": f"could not embed the query: {e}", "hits": []}

        if have_vec:
            rows = db.execute(
                """SELECT s.clip, s.idx, s.start, s.text, s.speaker, v.distance
                   FROM seg_vec v JOIN seg s ON s.id = v.rowid
                   WHERE v.embedding MATCH ? AND k = ?
                   ORDER BY v.distance""",
                (_pack(qv), limit)).fetchall()
            hits = [{"clip": r["clip"], "index": r["idx"], "start": r["start"],
                     "text": r["text"], "speaker": r["speaker"],
                     "score": round(1.0 - r["distance"], 4)} for r in rows]
        else:
            # Same answer without the extension, just linearly. Fine for the
            # thousands of lines this holds; the index earns its keep later.
            import math
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0
            scored = []
            for r in db.execute("SELECT clip, idx, start, text, speaker, vec FROM seg"):
                v = _unpack(r["vec"])
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                dot = sum(a * b for a, b in zip(qv, v))
                scored.append((dot / (qn * n), r))
            scored.sort(key=lambda x: -x[0])
            hits = [{"clip": r["clip"], "index": r["idx"], "start": r["start"],
                     "text": r["text"], "speaker": r["speaker"],
                     "score": round(sc, 4)} for sc, r in scored[:limit]]
        return {"hits": hits, "indexed": db.execute(
            "SELECT COUNT(*) c FROM seg").fetchone()["c"], "vec": have_vec}
    finally:
        db.close()


def remove_clip(name):
    """Drop a clip's lines from the index.

    Deleting a recording has to delete what it said too, or search keeps
    offering results that lead to a file that is no longer there.
    """
    db, have_vec = _connect()
    try:
        rows = db.execute("SELECT id FROM seg WHERE clip=?", (name,)).fetchall()
        if have_vec:
            for r in rows:
                db.execute("DELETE FROM seg_vec WHERE rowid=?", (r["id"],))
        db.execute("DELETE FROM seg WHERE clip=?", (name,))
        db.commit()
        return len(rows)
    finally:
        db.close()


def stats():
    db, have_vec = _connect()
    try:
        return {"lines": db.execute("SELECT COUNT(*) c FROM seg").fetchone()["c"],
                "clips": db.execute("SELECT COUNT(DISTINCT clip) c FROM seg").fetchone()["c"],
                "sqlite_vec": have_vec}
    finally:
        db.close()
