#!/usr/bin/env python3
"""
Local tool implementations for the agent. Everything is append-only JSONL
under data/agent/ -- no cloud, no external service.
"""

import contextlib
import fcntl
import json
import os
import time

# Derived from this file, not from the working directory. As a relative path
# it meant running any of these tools from somewhere else read and wrote a
# different data tree, silently.
STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "data", "agent")
STORE = os.path.normpath(STORE)


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
    r = _append("notes", {"title": title, "body": body, "tags": tags or []})
    return {"ok": True, "saved": "note", "title": r["title"]}


def add_task(text, due=None, owner=None):
    """Save an action item / to-do."""
    r = _append("tasks", {"text": text, "due": due, "owner": owner})
    return {"ok": True, "saved": "task", "text": r["text"]}


def add_calendar_event(title, start, end=None, attendees=None):
    """Save a calendar event mentioned in conversation."""
    r = _append("events", {"title": title, "start": start, "end": end,
                           "attendees": attendees or []})
    return {"ok": True, "saved": "event", "title": r["title"], "start": r["start"]}


def remember_fact(subject, fact):
    """Save a durable fact about a person or project."""
    r = _append("facts", {"subject": subject, "fact": fact})
    return {"ok": True, "saved": "fact", "subject": r["subject"]}


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
}

SCHEMAS = [
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
