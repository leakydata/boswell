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

    # How confident the sound tagger was that a person was audible, kept on
    # the clip row so "no transcript, but something was talking" is a query
    # rather than a scan of every transcript on disk. Added after the fact,
    # so it is a migration: sqlite has no ADD COLUMN IF NOT EXISTS.
    have = {r["name"] for r in c.execute("PRAGMA table_info(clips)")}
    if "voice_tag" not in have:
        c.execute("ALTER TABLE clips ADD COLUMN voice_tag REAL")
        c.commit()
    # What the sound tagger heard, as names, so "which clips have a dog in
    # them" is a query. The scores stay in the transcript; this is only for
    # finding things, and a clip with Dog at 0.11 is not a clip about a dog.
    if "sounds" not in have:
        c.execute("ALTER TABLE clips ADD COLUMN sounds TEXT")
        c.commit()
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
    voice_tag = None
    sounds = None
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
            # AST's own opinion about whether a person was audible, which is a
            # different question from whether the transcriber wrote anything.
            # Where the two disagree is exactly the list worth looking at.
            # Tags the owner has said are wrong are gone from every view:
            # search, filters, the notable-sounds list and the cleanup groups.
            # A tag is for finding things, so a wrong one is worse than a
            # missing one -- it puts a clip in front of you under a name that
            # is not what happened. Kept in the transcript rather than deleted,
            # so a correction survives re-transcription and can be undone.
            dropped = set(t.get("sounds_removed") or [])
            # A row is [name, score] or [name, score, when]: the tagger
            # started reporting where in the clip it heard the thing, and every
            # transcript written before that has the shorter form.
            kept = [(row[0], float(row[1])) for row in (t.get("sounds") or [])
                    if row and row[0] not in dropped]
            voice_tag = max([v for n, v in kept if n in VOICE_TAGS] or [0.0])
            heard = [n for n, v in kept if v >= SOUND_FLOOR]
            sounds = "\n".join(heard) if heard else None
        except Exception:
            status = "error"

    c.execute("""INSERT INTO clips(name, seconds, modified, status, has_speech,
                                   edited, speakers, preview, indexed_at,
                                   voice_tag, sounds)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(name) DO UPDATE SET
                   seconds=excluded.seconds, modified=excluded.modified,
                   status=excluded.status, has_speech=excluded.has_speech,
                   edited=excluded.edited, speakers=excluded.speakers,
                   preview=excluded.preview, indexed_at=excluded.indexed_at,
                   voice_tag=excluded.voice_tag,
                   sounds=excluded.sounds""",
              (name, seconds, os.path.getmtime(wav), status, has_speech,
               edited, json.dumps(speakers), preview, time.time(), voice_tag,
               sounds))
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


def implausible_span(started, ended, seconds):
    """Whether a clip claims more wall time than it could possibly cover.

    With voice-activity gating off, a clip's frames are continuous and its span
    should equal its audio. Dropped BLE frames can stretch it legitimately --
    the audio that arrived covers a wider capture window with holes in it -- so
    the bound is loose enough to leave those alone.

    What it catches is a stale device counter on one frame. A 30-second clip
    was written claiming to run from 23:48 to 10:24 the next morning: 38151
    seconds of span for 30 of audio. Conversations are grouped by the gaps
    between clips, so that one interval bridged the night and merged 333 clips
    into a single 14-hour "conversation" holding 167 minutes of audio.
    """
    if not seconds:
        return False
    return (ended - started) > max(seconds * 4, seconds + 300)


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
            started, ended = float(d["started"]), float(d["ended"])
            secs = float(d.get("seconds") or 0)
            # Checked at the reader as well as the writer: records made before
            # the writer knew to check are still on disk, and one of them is
            # enough to merge a whole night into a single conversation.
            if implausible_span(started, ended, secs):
                started = ended - secs
            return started, ended
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
             "preview": r["preview"] or "",
             # What the sound tagger thought, so the interface can show where
             # it disagrees with the transcriber without a second request.
             "voice_tag": (None if r["voice_tag"] is None
                           else round(float(r["voice_tag"]), 3)),
             "sounds": (r["sounds"] or "").split("\n") if r["sounds"] else []}
            for r in rows]


def clips_by_name(names):
    """Look up several clips at once, keyed by name.

    Meaning search returns clip names and nothing else, so a result found only
    that way arrived at the interface without the modified time or duration
    every row is rendered with -- and the date filter, which reads that time,
    would have quietly dropped exactly the results keyword search could not
    find.
    """
    names = [n for n in (names or []) if n]
    if not names:
        return {}
    c = _conn()
    out = {}
    # Chunked, because SQLite has a limit on how many parameters one statement
    # can carry and a search can return more names than that.
    for i in range(0, len(names), 400):
        chunk = names[i:i + 400]
        q = ",".join("?" for _ in chunk)
        for r in c.execute(f"SELECT * FROM clips WHERE name IN ({q})", chunk):
            out[r["name"]] = {
                "name": r["name"], "seconds": r["seconds"],
                "modified": r["modified"], "status": r["status"],
                "has_speech": None if r["has_speech"] is None else bool(r["has_speech"]),
                "edited": bool(r["edited"]),
                "speakers": json.loads(r["speakers"] or "[]"),
                "preview": r["preview"] or ""}
    return out


def fts_query(query):
    """Turn what somebody typed into an FTS5 MATCH expression.

    Each word becomes a quoted phrase so that FTS operators typed by accident
    are searched for rather than executed. The quotes themselves have to be
    doubled: without that, searching for a word containing an apostrophe or a
    quotation mark -- which transcripts of speech are full of -- ended the
    phrase early and raised sqlite3.OperationalError as a 500.
    """
    return " ".join('"' + w.replace('"', '""') + '"'
                    for w in query.split() if w)


def search(query, limit=200):
    """Full text over every segment, not just the preview."""
    c = _conn()
    q = fts_query(query)
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


# Sound classes that mean a person was audible. Kept here rather than imported
# from pipeline so indexing does not pull in torch.
VOICE_TAGS = ("Speech", "Conversation", "Male speech, man speaking",
              "Female speech, woman speaking", "Child speech, kid speaking",
              "Narration, monologue", "Whispering")


def missed_voice(limit=200, floor=0.05):
    """Clips the sound tagger heard a voice in and the transcriber did not.

    Two models trained on different tasks disagreeing about whether anyone
    spoke. Measured, the disagreement is worth listening to: of the ones AST
    was confident about, a re-run with the level fixed recovered real speech
    in two thirds of them. This is the list to go through by hand -- the audio
    is there, and nothing else is going to tell you what is on it.
    """
    c = _conn()
    rows = c.execute(
        """SELECT name, seconds, modified, status, voice_tag
           FROM clips
           WHERE (has_speech IS NULL OR has_speech = 0)
             AND voice_tag IS NOT NULL AND voice_tag >= ?
           ORDER BY voice_tag DESC, modified DESC
           LIMIT ?""", (floor, limit)).fetchall()
    return [dict(r) for r in rows]


# Below this the tagger is guessing. Chosen to match what the report tool
# treats as a real event rather than ambient noise.
SOUND_FLOOR = 0.20


def sound_vocabulary(limit=40):
    """Every sound the archive actually contains, commonest first.

    Built from what is there rather than from AudioSet's 527 classes, because
    a menu offering Didgeridoo and Theremin to somebody whose recordings
    contain a dog, a keyboard and a fan is a menu nobody reads.
    """
    c = _conn()
    counts = {}
    for r in c.execute("SELECT sounds FROM clips WHERE sounds IS NOT NULL"):
        for n in (r["sounds"] or "").split("\n"):
            if n:
                counts[n] = counts.get(n, 0) + 1
    out = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"name": n, "clips": k} for n, k in out[:limit]]


# A voice at any strength keeps a clip out of the cleanup entirely. Lower than
# SOUND_FLOOR on purpose: the floor decides what is worth showing, this decides
# what is safe to destroy, and those are not the same threshold.
CLEANUP_VOICE_FLOOR = 0.05


def cleanup_groups():
    """Silent clips, grouped by the exact set of sounds heard in them.

    The set, not the individual tags. "Delete anything tagged Typing" would
    take a clip tagged Typing AND Dog, and the dog is the reason that clip
    exists. Only a clip whose whole description is things you have called
    uninteresting can be uninteresting.

    Anything with a voice in it is excluded here rather than filtered later,
    so no combination of choices in the interface can reach one.
    """
    c = _conn()
    rows = c.execute(
        """SELECT name, seconds, sounds, voice_tag FROM clips
           WHERE has_speech = 0
             AND (voice_tag IS NULL OR voice_tag < ?)""",
        (CLEANUP_VOICE_FLOOR,)).fetchall()
    groups = {}
    for r in rows:
        names = tuple(sorted(n for n in (r["sounds"] or "").split("\n") if n))
        if not names:
            continue          # nothing was heard and nothing examined it; keep
        g = groups.setdefault(names, {"tags": list(names), "clips": 0,
                                      "seconds": 0.0, "names": []})
        g["clips"] += 1
        g["seconds"] += float(r["seconds"] or 0)
        if len(g["names"]) < 200:
            g["names"].append(r["name"])
    out = sorted(groups.values(), key=lambda g: -g["clips"])
    for g in out:
        g["seconds"] = round(g["seconds"], 1)
    return out


def clips_for_sound_sets(sets):
    """Names of the silent clips whose whole tag set is one of `sets`.

    Recomputed here rather than trusting a list of names from the browser: the
    caller is about to delete them.
    """
    want = {tuple(sorted(s)) for s in sets}
    return [g["names"] for g in cleanup_groups()
            if tuple(sorted(g["tags"])) in want]


# Sounds that are the room rather than an event. Present in almost every
# recording, and no more interesting than the air.
AMBIENT_SOUNDS = {
    "Silence", "White noise", "Pink noise", "Noise", "Environmental noise",
    "Static", "Hum", "Mains hum", "Sine wave", "Sonar",
    "Wind", "Wind noise (microphone)", "Mechanical fan", "Air conditioning",
    "Inside, small room", "Inside, large room or hall", "Outside, urban or manmade",
    "Tick", "Tick-tock", "Clock",
    "Computer keyboard", "Typing", "Mouse", "Clicking", "Writing",
}


def notable_sounds(min_score=0.15):
    """Everything the tagger heard that is neither speech nor the room.

    Ordered so the rare things come first. A turkey heard once is the reason
    to look at this list; Music heard twenty-eight times is not, and sorting
    by count would bury the turkey under it.
    """
    import json as _json
    c = _conn()
    rows = c.execute(
        "SELECT name, seconds, modified, sounds, preview FROM clips "
        "WHERE sounds IS NOT NULL").fetchall()
    by_tag = {}
    for r in rows:
        names = [n for n in (r["sounds"] or "").split("\n") if n]
        notable = [n for n in names
                   if n not in AMBIENT_SOUNDS and n not in VOICE_TAGS]
        if not notable:
            continue
        for n in notable:
            by_tag.setdefault(n, []).append({
                "clip": r["name"], "seconds": r["seconds"],
                "modified": r["modified"], "preview": r["preview"] or "",
            })
    out = [{"tag": t, "clips": len(v), "examples": v} for t, v in by_tag.items()]
    # Rarest first, then alphabetically so the order does not shuffle between
    # loads for tags with the same count.
    out.sort(key=lambda g: (g["clips"], g["tag"]))
    return out
