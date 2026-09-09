#!/usr/bin/env python3
"""
Local tool implementations for the agent. Everything is append-only JSONL
under data/agent/ -- no cloud, no external service.
"""

import contextlib
import fcntl
import json
import os
import re
import time

# Derived from this file, not from the working directory. As a relative path
# it meant running any of these tools from somewhere else read and wrote a
# different data tree, silently.
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "data", "agent")
STORE = os.path.normpath(STORE)

KINDS = ("tasks", "events", "notes", "facts", "topics")


def _store_path(kind):
    """Path for one kind, proven to sit directly in the store."""
    if kind not in KINDS:
        raise ValueError(f"unknown agent kind: {kind!r}")
    p = os.path.abspath(os.path.join(STORE, f"{kind}.jsonl"))
    if os.path.dirname(p) != os.path.abspath(STORE):
        raise ValueError(f"path escapes the agent store: {kind!r}")
    return p


@contextlib.contextmanager
def store_lock():
    """Serialise every writer of the agent store.

    Records are appended here while the edit, delete and clear paths rewrite
    the whole file and rename it into place. A rename replaces the file, so an
    append that lands after a rewriter has read the rows and before it renames
    goes away with the old inode -- the agent reports the item saved and it is
    not there. flock rather than a threading lock because the CLI in this
    directory and the web service are separate processes.
    """
    os.makedirs(STORE, exist_ok=True)
    path = os.path.join(STORE, ".lock")
    with open(path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# Which clips the review currently in progress is about.
#
# The tools are called by a model that has no idea what a clip is, so the
# caller sets this before running and the provenance is attached here. Without
# it an item is a sentence with no way back to the audio it came from -- you
# cannot listen to what was actually said, or tell whether the transcriber
# heard it right.
_context_clips = []


def set_context(clips):
    global _context_clips
    _context_clips = list(clips or [])


# The share of attributed speech that must come from a named person in the
# room before an item may be recorded about it.
#
# Measured over everything the agent has ever written to this archive. The one
# item that should never have been kept -- a full summary of a YouTuber's
# opinions on AI, filed as a note in a personal archive -- came from clips
# whose speech was 13.1% named person, 0% named media and 86.9% unnamed. Every
# item worth keeping came from clips at 99.7% or better. Nothing in between
# has been observed, so this sits far from both sides rather than close to the
# one example.
#
# Why not simply exclude speakers whose kind is media: because that label does
# not gate anything it has not been applied to. Of 86 voices in this archive
# 64 carry no kind at all, and the host of that AI video was never named --
# it was SPEAKER_00, which is why every existing filter, all of which match on
# names, let it through. Unnamed speech has to count against an item, not be
# invisible to the check.
NAMED_SHARE_MIN = 0.25

# Recording what a conversation was *about* is exempt. A topic tag on a video
# is honest and useful -- it says what was playing -- and does not claim
# anybody said, planned or committed to anything.
GATED_KINDS = ("notes", "tasks", "events", "facts")


def _speech_mix(clips):
    """(named person, named media, unnamed) segment counts over some clips."""
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    web = os.path.join(here, "..", "web")
    if web not in sys.path:
        sys.path.insert(0, web)
    import pipeline
    import speaker_store
    c = speaker_store._conn()
    try:
        media = {p["name"] for p in speaker_store.people(c)
                 if p.get("name") and p.get("kind") in
                 (speaker_store.KIND_MEDIA, speaker_store.KIND_IGNORED)}
    finally:
        c.close()
    person = off_screen = unnamed = 0
    for name in clips:
        tp = pipeline.transcript_path(name)
        if not os.path.exists(tp):
            continue
        try:
            t = json.load(open(tp))
        except Exception:
            continue
        table = t.get("speakers") or {}
        for seg in t.get("segments") or []:
            who = seg.get("speaker_name") or \
                (table.get(seg.get("speaker")) or {}).get("name")
            if not who:
                unnamed += 1
            elif who in media:
                off_screen += 1
            else:
                person += 1
    return person, off_screen, unnamed


def _attributable(kind):
    """Whether the clips in context are somebody's speech or something playing.

    Returns None when it may be recorded, or a refusal a model can act on.
    Best effort: if the transcripts cannot be read the write goes through, as
    it did before this existed.
    """
    if kind not in GATED_KINDS or not _context_clips:
        return None
    try:
        person, media, unnamed = _speech_mix(_context_clips)
    except Exception:
        return None
    total = person + media + unnamed
    if not total:
        return None
    share = person / total
    if share >= NAMED_SHARE_MIN:
        return None
    return {"ok": False, "error":
            f"only {share:.0%} of the speech in these clips is from a named "
            f"person ({person} segments named, {media} from a voice marked "
            f"media, {unnamed} unnamed). That is the shape of something "
            f"playing nearby rather than a conversation, and a personal "
            f"archive should not carry it as a fact, task, event or note. "
            f"Use tag_topics to say what was playing, or name the voice "
            f"first if somebody in the room was actually speaking."}


def _append(kind, record):
    os.makedirs(STORE, exist_ok=True)
    record = dict(record)
    if _context_clips:
        record["_clips"] = list(_context_clips)
    record["_recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # A stable id so a single item can be removed later. Line numbers shift
    # as soon as anything else is deleted, so they cannot serve as identity.
    record["_id"] = f"{int(time.time() * 1000):x}{os.urandom(2).hex()}"
    # Appended and flushed. A single small append is atomic enough for a file
    # read line by line: a torn last line is dropped by the reader and
    # everything before it is intact. Rewriting the file to add a record --
    # which is what the edit and delete paths do -- is the risky operation,
    # and those go through a rename.
    with store_lock():
        with open(os.path.join(STORE, f"{kind}.jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())

    # Into the agent's memory as well, so the next review can recall it.
    # Best effort: the item is already durable on disk, and an embedding
    # service that is down should not fail the tool call the model just made.
    try:
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        web = os.path.join(here, "..", "web")
        if web not in sys.path:
            sys.path.insert(0, web)
        import semantic
        semantic.index_item(dict(record, _kind=kind))
    except Exception:
        pass
    return record


def add_note(title, body, tags=None):
    """Save a note extracted from conversation."""
    blocked = _attributable("notes")
    if blocked:
        return blocked
    r = _append("notes", {"title": title, "body": body, "tags": tags or []})
    return {"ok": True, "saved": "note", "title": r["title"]}


def add_task(text, due=None, owner=None):
    """Save an action item / to-do."""
    blocked = _attributable("tasks")
    if blocked:
        return blocked
    r = _append("tasks", {"text": text, "due": due, "owner": owner})
    return {"ok": True, "saved": "task", "text": r["text"]}


def add_calendar_event(title, start, end=None, attendees=None):
    """Save a calendar event mentioned in conversation."""
    blocked = _attributable("events")
    if blocked:
        return blocked
    r = _append("events", {"title": title, "start": start, "end": end,
                           "attendees": attendees or []})
    return {"ok": True, "saved": "event", "title": r["title"], "start": r["start"]}


# A diarizer label, which is a position in one conversation and not a person.
#
# SPEAKER_00 is whoever the clustering happened to number first in *this*
# recording; tomorrow it is somebody else. A durable fact filed under it is
# worse than no fact -- it reads as knowledge about a named person and points
# at nobody.
#
# It is also where the media filter leaks. That filter marks lines as [MEDIA]
# by matching the speaker's *name* against the people store, so a YouTube
# host who has not been named yet is not filtered, and the first review of
# this archive duly recorded "Is a firmware engineer by trade" about
# SPEAKER_00 -- a claim made by a video playing near the microphone. The
# model was not wrong about what it read; it should not have been shown it as
# a person. Refusing the unresolved label closes the half of that which can
# be closed here, and says why, so the model records the same thing as a note
# instead of silently losing it.
UNRESOLVED = re.compile(r"^\s*(SPEAKER|speaker)[ _-]?\d+\s*$")


def remember_fact(subject, fact):
    """Save a durable fact about a person or project."""
    blocked = _attributable("facts")
    if blocked:
        return blocked
    if UNRESOLVED.match(subject or ""):
        return {"ok": False, "error":
                f"{subject!r} is a diarizer label, not a person -- it means a "
                "different voice in every recording, so a fact filed under it "
                "cannot be found again. Name the person, or if the voice is "
                "unidentified use add_note instead."}
    r = _append("facts", {"subject": subject, "fact": fact})
    return {"ok": True, "saved": "fact", "subject": r["subject"]}


def _rewrite(kind, mutate):
    """Read, change, and replace one store file under the lock."""
    path = _store_path(kind)
    with store_lock():
        rows = []
        if os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        changed, rows = mutate(rows)
        if not changed:
            return False
        tmp = path + ".part"
        with open(tmp, "w") as f:
            f.write("".join(json.dumps(d) + "\n" for d in rows))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return True


def merge_items(kind, keep_id, drop_ids, text=None):
    """Fold duplicate entries into one.

    Recall shows the model what it has already recorded, which is what stops
    it writing the same thing again -- but it could only ever add. The
    entries made before recall existed are still there, and one of them is a
    single sentence about asphalt recorded four times in slightly different
    words. Without this the model can see the duplicates and do nothing about
    them.

    The surviving entry keeps its id and its provenance, gains the clips the
    dropped ones came from, and may have its wording replaced with a version
    that covers all of them.
    """
    if kind not in KINDS:
        return {"ok": False, "error": f"unknown kind {kind}"}
    if isinstance(drop_ids, str):
        drop_ids = [d.strip() for d in drop_ids.split(",") if d.strip()]
    drop = {d for d in (drop_ids or []) if d and d != keep_id}
    if not drop:
        return {"ok": False, "error": "nothing to merge"}

    merged_clips, found = [], {"keep": False, "dropped": 0}

    def mutate(rows):
        out = []
        for r in rows:
            if r.get("_id") in drop:
                merged_clips.extend(r.get("_clips") or [])
                found["dropped"] += 1
                continue
            out.append(r)
        for r in out:
            if r.get("_id") == keep_id:
                found["keep"] = True
                clips = list(r.get("_clips") or [])
                for c in merged_clips:
                    if c not in clips:
                        clips.append(c)
                if clips:
                    r["_clips"] = clips
                if text:
                    for field in ("fact", "text", "title", "body"):
                        if field in r:
                            r[field] = text
                            break
                r["_merged"] = int(r.get("_merged", 0)) + found["dropped"]
        return found["dropped"] > 0, out

    _rewrite(kind, mutate)
    if not found["keep"]:
        return {"ok": False, "error": f"keep_id {keep_id} not found in {kind}"}

    try:
        import sys
        web = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
        if web not in sys.path:
            sys.path.insert(0, web)
        import semantic
        for d in drop:
            semantic.remove_item(d)
        for r in _load_kind(kind):
            if r.get("_id") == keep_id:
                semantic.index_item(dict(r, _kind=kind))
                break
    except Exception:
        pass
    return {"ok": True, "merged": found["dropped"], "kept": keep_id}


def _load_kind(kind):
    path = _store_path(kind)
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def tag_topics(topics):
    """Label this conversation with the subjects it covers."""
    if isinstance(topics, str):
        topics = [t.strip() for t in topics.split(",")]
    clean = []
    for t in topics or []:
        t = str(t).strip().lower()
        # Short, plain labels. A "topic" that is really a sentence cannot be
        # matched against the next conversation about the same thing.
        if t and len(t) <= 40 and t not in clean:
            clean.append(t)
    if not clean:
        return {"ok": False, "error": "no usable topics"}
    r = _append("topics", {"topics": clean})
    return {"ok": True, "saved": "topics", "topics": r["topics"]}


REGISTRY = {
    "add_note": add_note,
    "add_task": add_task,
    "add_calendar_event": add_calendar_event,
    "remember_fact": remember_fact,
    "tag_topics": tag_topics,
    "merge_items": merge_items,
}

SCHEMAS = [
    {"type": "function", "function": {
        "name": "merge_items",
        "description": ("Fold duplicate entries shown under ALREADY KNOWN into "
                        "one. Use when the same thing was recorded more than "
                        "once. Give the id to keep and the ids to remove, and "
                        "optionally replacement wording that covers all of "
                        "them. Only merge entries that genuinely say the same "
                        "thing."),
        "parameters": {"type": "object", "properties": {
            "kind": {"type": "string",
                     "enum": ["tasks", "events", "notes", "facts", "topics"]},
            "keep_id": {"type": "string"},
            "drop_ids": {"type": "array", "items": {"type": "string"}},
            "text": {"type": "string",
                     "description": "optional replacement wording"}},
            "required": ["kind", "keep_id", "drop_ids"]}}},
    {"type": "function", "function": {
        "name": "tag_topics",
        "description": ("Label this conversation with the two to five subjects "
                        "it is actually about, as short lowercase phrases, so "
                        "later conversations on the same subject can be found "
                        "together. Examples: 'shoulder injury', 'dog training', "
                        "'asphalt experiment'."),
        "parameters": {"type": "object", "properties": {
            "topics": {"type": "array", "items": {"type": "string"}}},
            "required": ["topics"]}}},
    {"type": "function", "function": {
        "name": "add_note",
        "description": "Save a note capturing something discussed. Use for context worth keeping that is not an action item.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "Short title"},
            "body": {"type": "string", "description": "The note content"},
            "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "body"]}}},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "Save an action item someone committed to or was assigned.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "What needs doing"},
            "due": {"type": "string", "description": "Due date if stated, else omit"},
            "owner": {"type": "string", "description": "Who owns it, by speaker name"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "add_calendar_event",
        "description": "Save a meeting or deadline that was scheduled or referenced.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO date/time or description"},
            "end": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "start"]}}},
    {"type": "function", "function": {
        "name": "remember_fact",
        "description": "Save a durable fact about a person, project, or preference worth recalling later.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "Person or project the fact is about"},
            "fact": {"type": "string"}},
            "required": ["subject", "fact"]}}},
]
