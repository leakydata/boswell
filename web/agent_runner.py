#!/usr/bin/env python3
"""
Runs the local LLM over finished conversations, without being asked.

Clips arrive every 30 seconds, which is far too small a unit to reason over —
half a sentence, no context. So transcripts accumulate instead, and the agent
fires when the conversation actually ends: a stretch of silence long enough
that whatever came before is complete. That produces one coherent pass over a
real exchange rather than sixty disconnected ones.
"""

import json
import os
import threading
import time

import atomicio
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
STORE = os.path.join(DATA, "agent")

# The only files the agent store contains. Every entry point resolves a kind
# through this: `kind` arrives from a query string, which may contain slashes,
# and it used to be joined straight into a path and passed to os.remove().
KINDS = ("tasks", "events", "notes", "facts")


def _kinds(kind):
    """Validate a caller-supplied kind, or return all of them."""
    if kind is None:
        return list(KINDS)
    if kind not in KINDS:
        raise ValueError(f"unknown agent kind: {kind!r}")
    return [kind]


def _store_path(kind):
    """Path for one kind, proven to sit directly in the agent store."""
    p = os.path.abspath(os.path.join(STORE, f"{kind}.jsonl"))
    if os.path.dirname(p) != os.path.abspath(STORE):
        raise ValueError(f"path escapes the agent store: {kind!r}")
    return p
OLLAMA = "http://localhost:11434/api/chat"

# gpt-oss:20b is ~13 GB and fits beside Whisper's ~9 GB on a 24 GB card.
# glm-4.7-flash is the stronger MoE but at 19 GB the two do not coexist.
DEFAULT_MODEL = "gpt-oss:20b"
IDLE_SECONDS = 90.0        # silence that marks the end of a conversation
MAX_WAIT = 900.0           # fire anyway if someone talks continuously
MIN_CHARS = 120            # below this there is nothing worth reasoning about
MAX_RETRIES = 3            # a batch survives this many failures before it is dropped
RETRY_BACKOFF = 30         # seconds, doubling
MAX_BACKOFF = 300
# A batch that is only a clip or two is thirty seconds of speech, and thirty
# seconds almost never contains a commitment, a date or a durable fact. The
# agent was reviewing exactly that and correctly answering "nothing to
# record" every time, so it produced nothing at all over a whole day. Batches
# are widened to the surrounding conversation before the model sees them.
CONTEXT_GAP = 300.0        # same gap the recordings view groups conversations by
# A long conversation can run to tens of thousands of characters. Past a point
# the model is not reading more, it is losing the beginning, so the tail is
# kept -- the most recent talk is the part most likely to contain something
# still worth acting on.
MAX_CHARS = 16000

SYSTEM = """You are reviewing a transcript of a real conversation captured by a \
wearable microphone. Speakers are named where known, or SPEAKER_xx where not.

Record only what was actually said and is worth keeping:
- something someone committed to doing  -> add_task
- a meeting, deadline or date mentioned -> add_calendar_event
- a durable fact about a person/project -> remember_fact
- context worth keeping that is none of the above -> add_note

Rules:
- Never invent details. If it was not said, do not record it.
- Skip smalltalk, filler and thinking aloud. Most conversation is not worth saving.
- If nothing is worth recording, call no tools and say so in one short sentence.
- Attribute owners by the speaker name shown.
- Transcription is imperfect; ignore garbled fragments rather than guessing."""


class ConversationAgent:
    def __init__(self, notify=None):
        self.notify = notify or (lambda *a, **k: None)
        self.model = DEFAULT_MODEL
        self.enabled = True
        self.idle_seconds = IDLE_SECONDS
        # Reentrant: status() holds the lock and calls pending_chars(), which
        # takes it again. A plain Lock deadlocks the request thread there.
        self._lock = threading.RLock()
        self._pending = []          # [(clip, [segments...], names)]
        self._first_at = None
        self._last_at = None
        self.busy = None
        self.last_result = None
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- intake -------------------------------------------------------
    def add(self, clip, segments, names):
        """Called when a clip finishes transcribing. Silence is ignored."""
        if not segments:
            return
        with self._lock:
            self._pending.append((clip, segments, names or {}))
            now = time.time()
            self._first_at = self._first_at or now
            self._last_at = now

    def pending_chars(self):
        with self._lock:
            return sum(len(s.get("text", "")) for _, segs, _ in self._pending for s in segs)

    def status(self):
        with self._lock:
            return {
                "enabled": self.enabled,
                "model": self.model,
                "pending_clips": len(self._pending),
                "pending_chars": self.pending_chars(),
                "seconds_idle": round(time.time() - self._last_at, 1) if self._last_at else None,
                "idle_seconds": self.idle_seconds,
                "busy": self.busy,
            }

    def review_now(self, batch):
        """Run the agent on an explicit batch and return what it did.

        Used by on-demand review; the scheduled path stays untouched so a
        manual review cannot disturb what is waiting to fire on its own.
        """
        return self._run(batch)

    def flush_now(self):
        with self._lock:
            self._last_at = 0        # make it look long idle; the loop picks it up

    # ---- scheduling ---------------------------------------------------
    def _loop(self):
        while True:
            time.sleep(3)
            if not self.enabled:
                continue
            with self._lock:
                if not self._pending:
                    continue
                idle = time.time() - (self._last_at or 0)
                waited = time.time() - (self._first_at or time.time())
                if idle < self.idle_seconds and waited < MAX_WAIT:
                    continue
                batch = self._pending
                self._pending = []
                self._first_at = self._last_at = None
            try:
                self.last_result = self._run(batch)
                self._failures = 0
            except Exception as e:
                # Put it back. The batch was removed from _pending before the
                # run, so an Ollama timeout used to discard the transcripts
                # permanently -- the work was gone and nothing said so.
                self._failures = getattr(self, "_failures", 0) + 1
                if self._failures <= MAX_RETRIES:
                    with self._lock:
                        self._pending = batch + self._pending
                        self._first_at = self._first_at or time.time()
                        self._last_at = time.time() + min(
                            RETRY_BACKOFF * 2 ** (self._failures - 1), MAX_BACKOFF)
                    self.notify("log", text=f"agent failed: {str(e)[:120]} "
                                            f"-- retrying ({self._failures}/{MAX_RETRIES})")
                else:
                    self.last_result = {"clips": [c for c, _, _ in batch],
                                        "actions": 0, "said": "", "at": time.time(),
                                        "skipped_reason": f"failed {self._failures} "
                                                          f"times: {str(e)[:120]}"}
                    self.notify("log", text=f"agent gave up on {len(batch)} clip(s) "
                                            f"after {self._failures} failures")
                    self._failures = 0

    # ---- execution ----------------------------------------------------
    def _render(self, batch):
        lines = []
        for clip, segs, names in batch:
            for s in segs:
                spk = s.get("speaker")
                who = s.get("speaker_name") or (names.get(spk, {}) or {}).get("name") or spk or "UNKNOWN"
                lines.append(f"{who}: {s['text'].strip()}")
        return "\n".join(lines)

    def _widen(self, batch):
        """Grow a batch to the conversation its clips belong to.

        What is worth recording is rarely visible in one clip: "I will send
        that on Friday" is a commitment only if you can see what "that" was.
        Neighbouring clips within CONTEXT_GAP are pulled in from their stored
        transcripts, which costs a few file reads and no re-transcription.
        """
        try:
            import index_db, pipeline
        except Exception:
            return batch
        have = {c for c, _, _ in batch}
        try:
            convs = index_db.conversations(int(CONTEXT_GAP), 400)
        except Exception:
            return batch

        wanted = []
        for conv in convs:
            if have & set(conv["clips"]):
                wanted = conv["clips"]
                break
        if not wanted:
            return batch

        widened = []
        for name in wanted:
            existing = next((b for b in batch if b[0] == name), None)
            if existing:
                widened.append(existing)
                continue
            tp = pipeline.transcript_path(name)
            if not os.path.exists(tp):
                continue
            try:
                t = json.load(open(tp))
            except Exception:
                continue
            segs = t.get("segments") or []
            if segs:
                widened.append((name, segs, t.get("speakers") or {}))
        return widened or batch

    def _run(self, batch):
        """Run one review and return what *this* invocation did.

        It used to return nothing and write to self.last_result, and it
        returned early on a short batch without touching that field -- so an
        on-demand review of a quiet conversation reported the result of some
        earlier one. A caller cannot tell an answer from a leftover, which is
        the one thing a result needs to be able to say.
        """
        import sys
        sys.path.insert(0, os.path.join(HERE, "..", "host"))
        from tools_impl import REGISTRY, SCHEMAS

        batch = self._widen(batch)
        clips = [c for c, _, _ in batch]
        text = self._render(batch)
        if len(text) < MIN_CHARS:
            return {"clips": clips, "actions": 0, "said": "", "at": time.time(),
                    "skipped_reason": f"only {len(text)} characters of speech; "
                                      f"{MIN_CHARS} needed"}
        if len(text) > MAX_CHARS:
            text = text[-MAX_CHARS:]
            text = text[text.index("\n") + 1:] if "\n" in text else text

        self.busy = f"{len(clips)} clip(s)"
        self.notify("agent", status="running", clips=len(clips), chars=len(text))

        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": f"Conversation transcript:\n\n{text}"}]
        actions, said = [], ""
        try:
            for _ in range(6):
                r = requests.post(OLLAMA, json={
                    "model": self.model, "messages": messages,
                    "tools": SCHEMAS, "stream": False,
                    "options": {"temperature": 0.2},
                }, timeout=600)
                r.raise_for_status()
                msg = r.json().get("message", {})
                messages.append(msg)
                said = msg.get("content") or said
                calls = msg.get("tool_calls") or []
                if not calls:
                    break
                for c in calls:
                    fn = c.get("function", {})
                    name = fn.get("name")
                    raw = fn.get("arguments", {})
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    if name not in REGISTRY:
                        result = {"ok": False, "error": f"unknown tool {name}"}
                    else:
                        try:
                            args.setdefault("_source_clips", clips) if False else None
                            result = REGISTRY[name](**args)
                        except Exception as e:
                            result = {"ok": False, "error": str(e)}
                    actions.append({"tool": name, "args": args, "result": result})
                    messages.append({"role": "tool", "name": name,
                                     "content": json.dumps(result)})
        finally:
            self.busy = None

        result = {"clips": clips, "actions": len(actions),
                  "said": said.strip()[:300], "at": time.time(),
                  "skipped_reason": None}
        self.notify("agent", status="done", clips=len(clips), actions=len(actions),
                    said=said.strip()[:200])
        if actions:
            self.notify("log", text=f"agent recorded {len(actions)} item(s) "
                                    f"from {len(clips)} clip(s)")
        return result


def _backfill_ids(kind):
    """Give ids to items written before they had one, once, in place."""
    p = os.path.join(STORE, f"{kind}.jsonl")
    if not os.path.exists(p):
        return
    rows, changed = [], False
    for line in open(p):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if "_id" not in d:
            d["_id"] = f"{int(time.time()*1000):x}{os.urandom(2).hex()}"
            changed = True
        rows.append(d)
    if changed:
        # Rewritten whole, so it goes through a temp file and a rename. A
        # crash partway through a direct rewrite does not lose one record --
        # it loses every task, note and fact the agent has ever kept.
        atomicio.write_text(p, "".join(json.dumps(d) + "\n" for d in rows))


def delete_item(kind, item_id):
    p = os.path.join(STORE, f"{kind}.jsonl")
    if not os.path.exists(p):
        return False
    rows, removed = [], False
    for line in open(p):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("_id") == item_id:
            removed = True
            continue
        rows.append(d)
    if removed:
        atomicio.write_text(p, "".join(json.dumps(d) + "\n" for d in rows))
    return removed


def clear_items(kind=None):
    kinds = _kinds(kind)
    n = 0
    for k in kinds:
        p = _store_path(k)
        if os.path.exists(p):
            n += sum(1 for line in open(p) if line.strip())
            os.remove(p)
    return n


def load_items(kind=None, limit=200):
    """Everything the agent has recorded, newest first."""
    kinds = _kinds(kind)
    for k in kinds:
        _backfill_ids(k)
    out = []
    for k in kinds:
        p = _store_path(k)
        if not os.path.exists(p):
            continue
        for line in open(p):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            d["_kind"] = k
            out.append(d)
    out.sort(key=lambda d: d.get("_recorded_at", ""), reverse=True)
    return out[:limit]
