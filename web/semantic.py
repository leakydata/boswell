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
    if have_vec:
        # Cosine, not the default L2. These embeddings are compared by
        # direction, and with L2 the reported distance ran past 2 so the
        # score came out negative and could not be ranked or shown.
        db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS seg_vec
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
    try:
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
            except Exception:
                continue
            cur = db.execute(
                """INSERT INTO seg(clip, idx, start, text, speaker, vec)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(clip, idx) DO UPDATE SET
                     text=excluded.text, speaker=excluded.speaker,
                     start=excluded.start, vec=excluded.vec""",
                (name, i, seg.get("start", 0.0), text, seg.get("speaker"),
                 _pack(v)))
            if have_vec:
                rid = cur.lastrowid or db.execute(
                    "SELECT id FROM seg WHERE clip=? AND idx=?",
                    (name, i)).fetchone()["id"]
                db.execute("DELETE FROM seg_vec WHERE rowid=?", (rid,))
                db.execute("INSERT INTO seg_vec(rowid, embedding) VALUES(?,?)",
                           (rid, _pack(v)))
            added += 1
        db.commit()
    finally:
        db.close()
    return added


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


def stats():
    db, have_vec = _connect()
    try:
        return {"lines": db.execute("SELECT COUNT(*) c FROM seg").fetchone()["c"],
                "clips": db.execute("SELECT COUNT(DISTINCT clip) c FROM seg").fetchone()["c"],
                "sqlite_vec": have_vec}
    finally:
        db.close()
