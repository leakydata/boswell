#!/usr/bin/env python3
"""
Local tool implementations for the agent. Everything is append-only JSONL
under data/agent/ -- no cloud, no external service.
"""

import json
import os
import time

STORE = "data/agent"


def _append(kind, record):
    os.makedirs(STORE, exist_ok=True)
    record = dict(record)
    record["_recorded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # A stable id so a single item can be removed later. Line numbers shift
    # as soon as anything else is deleted, so they cannot serve as identity.
    record["_id"] = f"{int(time.time() * 1000):x}{os.urandom(2).hex()}"
    with open(os.path.join(STORE, f"{kind}.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")
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


REGISTRY = {
    "add_note": add_note,
    "add_task": add_task,
    "add_calendar_event": add_calendar_event,
    "remember_fact": remember_fact,
}

SCHEMAS = [
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
