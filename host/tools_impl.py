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


# Who has to have said something before it may be recorded about them.
#
# The first version of this counted the *mix of the clips*: refuse when less
# than a quarter of the speech in them came from a named person. It was built
# on a real measurement -- the one item that should never have been kept came
# from clips 13.1% named-person, every good item from 99.7% or better -- and
# it was the wrong shape, which the measurement could not show because the
# archive had no example of the case that matters.
#
# The case that matters: Nathan talks at the screen while a video plays. His
# own words and the video's are in the same clips, and the video does most of
# the talking. Measured on the conversation where he said "I just want a way
# to have AI take notes from videos that I watch instead of just listening to
# them" -- the most useful sentence in the archive that evening, and the one
# thing a note-taker should not miss -- the clips are 4.0% named person. The
# mix rule refused it. A rule that blocks the owner's own dictated request
# because a robotics tutorial was louder is not a safety rule, it is a bug
# with a threshold.
#
# So the unit of evidence is the line, not the clip. An item names the person
# whose words it came from, and that person has to be somebody the archive can
# name and not a voice off a screen. The model has just read the transcript
# with speaker labels on every line; it is the one thing here that knows.
#
# The same reasoning as `clips`: provenance required rather than optional. An
# item that cannot say who said it is not attributable, and the store already
# has one round of history of what that produces.
GATED_KINDS = ("notes", "tasks", "events", "facts")

# Diarizer labels. A position in one recording, a different voice in the next.
UNRESOLVED = re.compile(r"^\s*(SPEAKER|speaker)[ _-]?\d+\s*$")


def _named_speakers(clips):
    """(people, off_screen) -- the names actually heard in these clips."""
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    web = os.path.join(here, "..", "web")
    if web not in sys.path:
        sys.path.insert(0, web)
    import pipeline
    import speaker_store
    c = speaker_store._conn()
    try:
        kind = {p["name"]: p.get("kind") for p in speaker_store.people(c)
                if p.get("name")}
    finally:
        c.close()
    people, off_screen = set(), set()
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
                continue
            if kind.get(who) in (speaker_store.KIND_MEDIA,
                                 speaker_store.KIND_IGNORED):
                off_screen.add(who)
            else:
                people.add(who)
    return people, off_screen


def _attributable(kind, said_by):
    """Whether `said_by` is somebody who can be quoted from these clips.

    Returns None when the item may be recorded, or a refusal a model can act
    on. Best effort on the reading: if the transcripts cannot be read the
    write goes through, as it did before this existed.
    """
    if kind not in GATED_KINDS:
        return None
    said_by = (said_by or "").strip()
    if not said_by:
        return {"ok": False, "error":
                "said_by is required -- the name of the person whose words "
                "this came from, as shown on the transcript line. This "
                "archive is full of video playing near the microphone, and "
                "an item that cannot say who said it cannot be told apart "
                "from something a podcast host asserted."}
    if UNRESOLVED.match(said_by):
        return {"ok": False, "error":
                f"{said_by!r} is a diarizer label, not a person: it means a "
                f"different voice in every recording. If somebody in the room "
                f"said this, name the voice first; if it was a video, use "
                f"tag_topics to say what was playing."}
    if not _context_clips:
        return None
    try:
        people, off_screen = _named_speakers(_context_clips)
    except Exception:
        return None
    if said_by in off_screen:
        return {"ok": False, "error":
                f"{said_by} is marked as audio off a screen. Nothing a video "
                f"says is a fact, task or event in a personal archive -- use "
                f"tag_topics to record what was playing."}
    if people and said_by not in people:
        return {"ok": False, "error":
                f"{said_by} does not speak in these clips. Named speakers "
                f"here: {sorted(people)}. Pass the clips the words are "
                f"actually in, or the name as the transcript shows it."}
    return None


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


def add_note(title, body, said_by=None, tags=None):
    """Save a note extracted from conversation."""
    blocked = _attributable("notes", said_by)
    if blocked:
        return blocked
    r = _append("notes", {"title": title, "body": body, "said_by": said_by,
                          "tags": tags or []})
    return {"ok": True, "saved": "note", "title": r["title"]}


def add_task(text, said_by=None, due=None, owner=None):
    """Save an action item / to-do."""
    blocked = _attributable("tasks", said_by)
    if blocked:
        return blocked
    r = _append("tasks", {"text": text, "due": due,
                          "owner": owner or said_by, "said_by": said_by})
    return {"ok": True, "saved": "task", "text": r["text"]}


def add_calendar_event(title, start, said_by=None, end=None, attendees=None):
    """Save a calendar event mentioned in conversation."""
    blocked = _attributable("events", said_by)
    if blocked:
        return blocked
    r = _append("events", {"title": title, "start": start, "end": end,
                           "said_by": said_by, "attendees": attendees or []})
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


def remember_fact(subject, fact, said_by=None):
    """Save a durable fact about a person or project.

    `subject` is who the fact is about; `said_by` is who said it. They are
    often different and both matter: "Blase is moving in March" said by Blase
    is a fact, and said by a podcast is not.
    """
    blocked = _attributable("facts", said_by)
    if blocked:
        return blocked
    if UNRESOLVED.match(subject or ""):
        return {"ok": False, "error":
                f"{subject!r} is a diarizer label, not a person -- it means a "
                "different voice in every recording, so a fact filed under it "
                "cannot be found again. Name the person, or if the voice is "
                "unidentified use add_note instead."}
    r = _append("facts", {"subject": subject, "fact": fact,
                          "said_by": said_by})
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
            "said_by": {"type": "string", "description": "The name on the transcript line these words came from. Required: this archive contains video playing near the microphone, often in the same clips as the wearer talking back at it, so who said a thing is not recoverable from the clip it is in."},
            "tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "body", "said_by"]}}},
    {"type": "function", "function": {
        "name": "add_task",
        "description": "Save an action item someone committed to or was assigned.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "What needs doing"},
            "due": {"type": "string", "description": "Due date if stated, else omit"},
            "owner": {"type": "string", "description": "Who owns it, by speaker name"},
            "said_by": {"type": "string", "description": "The name on the transcript line these words came from. Required: this archive contains video playing near the microphone, often in the same clips as the wearer talking back at it, so who said a thing is not recoverable from the clip it is in."}},
            "required": ["text", "said_by"]}}},
    {"type": "function", "function": {
        "name": "add_calendar_event",
        "description": "Save a meeting or deadline that was scheduled or referenced.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "ISO date/time or description"},
            "end": {"type": "string"},
            "attendees": {"type": "array", "items": {"type": "string"}},
            "said_by": {"type": "string", "description": "The name on the transcript line these words came from. Required: this archive contains video playing near the microphone, often in the same clips as the wearer talking back at it, so who said a thing is not recoverable from the clip it is in."}},
            "required": ["title", "start", "said_by"]}}},
    {"type": "function", "function": {
        "name": "remember_fact",
        "description": "Save a durable fact about a person, project, or preference worth recalling later.",
        "parameters": {"type": "object", "properties": {
            "subject": {"type": "string", "description": "Person or project the fact is about"},
            "fact": {"type": "string"},
            "said_by": {"type": "string", "description": "The name on the transcript line these words came from. Required: this archive contains video playing near the microphone, often in the same clips as the wearer talking back at it, so who said a thing is not recoverable from the clip it is in."}},
            "required": ["subject", "fact", "said_by"]}}},
]
