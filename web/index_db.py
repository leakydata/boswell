#!/usr/bin/env python3
"""
An index over clips and transcript segments.

Listing recordings used to open and parse every transcript on disk on every
request. That is fine at ten clips, noticeable at a hundred and untenable at a
few thousand — about a week of continuous use. Search had the same shape of
problem in a worse way: it matched against a 180-character preview, so a word
spoken thirty seconds into a conversation could not be found. It looked like
full-text search and was not.

SQLite earns its place here as an index, not as a store. The files on disk stay
authoritative and human-readable; this can be deleted at any time and rebuilt.
"""

import json
import os
import sqlite3
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
DB_PATH = os.path.join(DATA, "index.db")

_local = threading.local()


def _conn():
    if getattr(_local, "conn", None) is None:
        os.makedirs(DATA, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
        _ensure(c)
    return _local.conn


def _ensure(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS clips (
        name       TEXT PRIMARY KEY,
        seconds    REAL,
        modified   REAL,
        status     TEXT,
        has_speech INTEGER,          -- NULL when not transcribed yet
        edited     INTEGER DEFAULT 0,
        speakers   TEXT,             -- JSON array of resolved names
        preview    TEXT,
        indexed_at REAL
    );
    CREATE INDEX IF NOT EXISTS clips_modified ON clips(modified DESC);

    CREATE VIRTUAL TABLE IF NOT EXISTS segments USING fts5(
        clip UNINDEXED, start UNINDEXED, "end" UNINDEXED,
        speaker UNINDEXED, text,
        tokenize='porter unicode61'
    );
    """)
    c.commit()


# ---------------------------------------------------------------- writing

def upsert_clip(name, transcript_path=None, wav_path=None):
    """Index one clip from its files. Cheap enough to call on every change."""
    import soundfile as sf
    c = _conn()
    wav = wav_path or os.path.join(DATA, name)
    if not os.path.exists(wav):
        remove_clip(name)
        return
    try:
        seconds = round(sf.info(wav).duration, 1)
    except Exception:
        seconds = 0.0

    tp = transcript_path or os.path.join(DATA, "transcripts",
                                         os.path.splitext(name)[0] + ".json")
    status, has_speech, edited, speakers, preview = "none", None, 0, [], ""
    segs = []
    if os.path.exists(tp):
        try:
            t = json.load(open(tp))
            segs = t.get("segments", [])
            resolved = t.get("speakers") or {}
            seen = []
            for x in segs:
                sp = x.get("speaker")
                if not sp:
                    continue
                nm = x.get("speaker_name") or (resolved.get(sp) or {}).get("name") or "unknown"
                if nm not in seen:
                    seen.append(nm)
            speakers = seen
            preview = " ".join(x["text"] for x in segs)[:180]
            edited = 1 if t.get("edited") else 0
            status = "done"
            has_speech = 1 if preview.strip() else 0
        except Exception:
            status = "error"

    c.execute("""INSERT INTO clips(name, seconds, modified, status, has_speech,
                                   edited, speakers, preview, indexed_at)
                 VALUES(?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(name) DO UPDATE SET
                   seconds=excluded.seconds, modified=excluded.modified,
                   status=excluded.status, has_speech=excluded.has_speech,
                   edited=excluded.edited, speakers=excluded.speakers,
                   preview=excluded.preview, indexed_at=excluded.indexed_at""",
              (name, seconds, os.path.getmtime(wav), status, has_speech,
               edited, json.dumps(speakers), preview, time.time()))
    c.execute("DELETE FROM segments WHERE clip = ?", (name,))
    if segs:
        c.executemany(
            'INSERT INTO segments(clip, start, "end", speaker, text) VALUES(?,?,?,?,?)',
            [(name, s.get("start"), s.get("end"), s.get("speaker"), s.get("text", ""))
             for s in segs if s.get("text")])
    c.commit()


def remove_clip(name):
    c = _conn()
    c.execute("DELETE FROM clips WHERE name = ?", (name,))
    c.execute("DELETE FROM segments WHERE clip = ?", (name,))
    c.commit()


def device_times(name):
    """When the device says this recording started and ended, if it said.

    Preferred over file metadata everywhere it exists: the device's clock is
    the only witness that was actually present when the audio happened.
    """
    p = os.path.join(DATA, "times", name + ".json")
    if not os.path.exists(p):
        return None
    try:
        d = json.load(open(p))
        if "started" in d and "ended" in d:
            return float(d["started"]), float(d["ended"])
    except Exception:
        pass
    return None


def sync():
    """Reconcile the index with what is actually on disk.

    The files are the source of truth, so anything can be moved or deleted
    outside the app and this puts the index right again.
    """
    c = _conn()
    on_disk = {f for f in os.listdir(DATA)} if os.path.isdir(DATA) else set()
    wavs = {f for f in on_disk if f.endswith(".wav")}
    known = {r["name"]: (r["indexed_at"] or 0, r["modified"] or 0)
             for r in c.execute("SELECT name, indexed_at, modified FROM clips")}

    for gone in set(known) - wavs:
        remove_clip(gone)
    added = 0
    for name in wavs:
        wav = os.path.join(DATA, name)
        tp = os.path.join(DATA, "transcripts", os.path.splitext(name)[0] + ".json")
        newest = os.path.getmtime(wav)
        if os.path.exists(tp):
            newest = max(newest, os.path.getmtime(tp))
        seen_at, seen_mtime = known.get(name, (0, 0))
        # Re-index if the file is newer than the last pass, OR if its
        # timestamp has changed at all. A file whose mtime moved BACKWARDS is
        # invisible to a "newer than" test: restamping recovered clips to
        # their true capture time left the index holding the old drain-time
        # values, and clips came out of the conversation in the wrong order
        # while every file on disk was correct.
        if seen_at < newest or abs(seen_mtime - os.path.getmtime(wav)) > 0.5:
            upsert_clip(name)
            added += 1
    return {"indexed": added, "removed": len(set(known) - wavs), "total": len(wavs)}


# ---------------------------------------------------------------- reading

def list_clips(limit=1000):
    c = _conn()
    rows = c.execute("""SELECT * FROM clips ORDER BY modified DESC LIMIT ?""", (limit,))
    return [{"name": r["name"], "seconds": r["seconds"], "modified": r["modified"],
             "status": r["status"],
             "has_speech": None if r["has_speech"] is None else bool(r["has_speech"]),
             "edited": bool(r["edited"]),
             "speakers": json.loads(r["speakers"] or "[]"),
             "preview": r["preview"] or ""} for r in rows]


def search(query, limit=200):
    """Full text over every segment, not just the preview."""
    c = _conn()
    q = " ".join(f'"{w}"' for w in query.split() if w)
    if not q:
        return []
    rows = c.execute("""
        SELECT s.clip, s.start, s."end", s.speaker,
               snippet(segments, 4, '<mark>', '</mark>', '…', 12) AS snip,
               c.modified, c.seconds
        FROM segments s JOIN clips c ON c.name = s.clip
        WHERE segments MATCH ?
        ORDER BY rank LIMIT ?""", (q, limit))
    out = {}
    for r in rows:
        e = out.setdefault(r["clip"], {"name": r["clip"], "modified": r["modified"],
                                       "seconds": r["seconds"], "hits": []})
        e["hits"].append({"start": r["start"], "end": r["end"],
                          "speaker": r["speaker"], "snippet": r["snip"]})
    return sorted(out.values(),
                  key=lambda d: d["modified"] - (d.get("seconds") or 0),
                  reverse=True)


def conversations(gap_seconds=300, limit=400):
    """Group clips into conversations.

    A 30-second clip is a storage unit, not a human one. What someone
    remembers is "the conversation with Blase this morning", so contiguous
    clips are grouped and a gap longer than `gap_seconds` starts a new one.
    """
    clips = list_clips(limit)
    # Order by when each clip STARTED, not when it finished.
    #
    # modified is the end of the audio, and clips are not all the same length:
    # a 10 s clip that began later can finish before a 30 s clip that began
    # earlier. Sorting on the end time therefore put recovered audio out of
    # sequence against the live clips around it -- 20 clips out of 210 in one
    # measurement, all of them pairs whose durations differed.
    def started_at(c):
        t = device_times(c["name"])
        if t:
            return t[0]
        # No device record: fall back to the file, which is what every clip
        # recorded before this existed has.
        return c["modified"] - (c["seconds"] or 0)

    def ended_at(c):
        t = device_times(c["name"])
        return t[1] if t else c["modified"]

    clips.sort(key=started_at)
    groups = []
    for c in clips:
        # Grouping uses the same clock the sort does. Ordering by device time
        # while deciding conversation boundaries from file mtime meant a
        # recovered clip could be placed correctly in the sequence and still
        # fall into the wrong conversation.
        start = started_at(c)
        if groups and start - groups[-1]["end"] <= gap_seconds:
            g = groups[-1]
        else:
            g = {"start": start, "end": start, "clips": [], "speakers": [],
                 "seconds": 0.0, "preview": "", "with_speech": 0}
            groups.append(g)
        g["end"] = max(g["end"], ended_at(c))
        g["clips"].append(c["name"])
        g["seconds"] += c["seconds"] or 0
        if c["has_speech"]:
            g["with_speech"] += 1
        for sp in c["speakers"]:
            if sp not in g["speakers"]:
                g["speakers"].append(sp)
        if c["preview"].strip() and len(g["preview"]) < 240:
            g["preview"] = (g["preview"] + " " + c["preview"]).strip()[:240]
    for g in groups:
        g["seconds"] = round(g["seconds"], 1)
        g["span"] = round(g["end"] - g["start"], 1)
    return list(reversed(groups))


def stats():
    c = _conn()
    r = c.execute("""SELECT COUNT(*) n, COALESCE(SUM(seconds),0) secs,
                            SUM(has_speech=1) with_speech
                     FROM clips""").fetchone()
    segs = c.execute("SELECT COUNT(*) n FROM segments").fetchone()["n"]
    return {"clips": r["n"], "seconds": round(r["secs"] or 0, 1),
            "with_speech": r["with_speech"] or 0, "segments": segs}
