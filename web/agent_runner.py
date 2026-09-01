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
KINDS = ("tasks", "events", "notes", "facts", "topics")


def _store_lock():
    """The lock tools_impl uses for appends, so rewrites cannot straddle one.

    Read-modify-write of the whole file, renamed into place, against another
    process appending to it: without a shared lock an append landing between
    the read and the rename disappears with the old inode.
    """
    import sys
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    from tools_impl import store_lock
    return store_lock()


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
# Silence that marks the end of a conversation.
#
# Ordering: consolidation settles at 150 s on a loop ticking every 120 s, so it
# lands somewhere between 150 s and 270 s after the last clip. At 90 s the agent
# read every conversation before that, while its speaker labels were still the
# per-clip SPEAKER_00 the diarizer produced rather than the names consolidation
# resolves -- so it wrote facts attributing things to nobody.
#
# That also silently defeated the media filter in _render(), which matches on
# names: a YouTube host not yet resolved to "Ryan Long" is only SPEAKER_00, and
# his claims went into the fact store as the user's own. Both fixes are one
# fix, and this is the half that has to come first.
#
# 330 s clears the far end of the consolidation window with a minute to spare.
# The cost is a review landing about four minutes after you stop talking
# instead of ninety seconds; the benefit is that it lands with names attached.
# data/prefs.json carries the live value and overrides this at startup
# (server.py), so changing this constant alone would have done nothing.
IDLE_SECONDS = 330.0
MAX_WAIT = 900.0           # fire anyway if someone talks continuously
MIN_CHARS = 120            # below this there is nothing worth reasoning about
MAX_RETRIES = 3            # a batch survives this many failures before it is dropped
RETRY_BACKOFF = 30         # seconds, doubling
MAX_BACKOFF = 300
# How much of the transcript to search memory with, how many entries to show,
# and how close they must be. Loose enough to catch a rephrasing, tight enough
# that unrelated recall does not crowd out the transcript.
RECALL_QUERY_CHARS = 2000
RECALL_ITEMS = 12
RECALL_MIN_SCORE = 0.55
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
- Always call tag_topics once, whatever else you do. It labels what this
  conversation was about so later conversations on the same subject can be
  found with it, and that is worth doing even when there is nothing new to
  record.
- If nothing else is worth recording, say so in one short sentence.
- Attribute owners by the speaker name shown.
- Transcription is imperfect; ignore garbled fragments rather than guessing.
- Lines marked [MEDIA] are audio playing near the microphone -- video,
  podcast, music -- not people in the room. Never record a task, fact or
  event from them, and never attribute anything to a [MEDIA] speaker. They
  are shown only so you can follow what the real speakers are reacting to.

You may be shown ALREADY KNOWN entries retrieved from earlier conversations.
They are what you have recorded before. Use them:
- Do not record something you already know. Say so instead.
- If this conversation adds to one, record the new part only, and say which
  entry it extends.
- If it contradicts one, record the correction and say what changed.
- Use the same subject name an existing entry uses for the same person or
  project, so one subject does not end up split across several names.
- If two or more ALREADY KNOWN entries of the same kind say the same thing,
  call merge_items once to fold them into one, using the ids shown. Merge
  only entries that genuinely duplicate each other -- a fact that adds detail
  is not a duplicate of the one it adds to. Tidying these up is part of the
  job: they exist because earlier reviews could not see what had already been
  recorded."""


def _media_names():
    """Names whose speech is playback, not someone in the room.

    The archive fills up with YouTube: a video plays near the microphone, the
    diarizer clusters the host as a speaker, and someone names that cluster so
    the transcript reads properly. Right for a transcript, wrong here -- the
    agent had no way to tell a host's claim from the user's own, and recorded
    "planning to wire a sensor into a backpack for a 14-mile race at altitude"
    as a durable fact about the user. It was a fact about Data Slayer.

    Keyed on kind, not the free-text role: role is a human label nobody promised
    to keep in any particular form, while kind is the field the store already
    trusts to decide whether a voice may write its own name onto a clip.

    Best effort. If the store cannot be read the review still runs unfiltered,
    which is what it did before this existed.
    """
    try:
        import speaker_store
    except Exception:
        return frozenset()
    try:
        return frozenset(
            p["name"] for p in speaker_store.people()
            if p.get("name") and p.get("kind") == speaker_store.KIND_MEDIA)
    except Exception:
        return frozenset()


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
    def _recall(self, text):
        """What is already recorded that bears on this conversation.

        Without this every review started from nothing: the model saw a system
        prompt and a transcript and had no way to know it had written the same
        thing down before. The store shows the cost -- one goal recorded four
        times in slightly different words, a shoulder injury as three
        unrelated facts, and one person filed under two different subjects
        because nothing told the model which name it had used last time.

        Best effort. If the embedding service is down the review still runs,
        with the memory it used to have, which is none.
        """
        try:
            import semantic
        except Exception:
            return ""
        try:
            # The end of the transcript, which is what the review is about.
            r = semantic.recall(text[-RECALL_QUERY_CHARS:], limit=RECALL_ITEMS)
        except Exception as e:
            self.notify("log", text=f"recall unavailable: {str(e)[:80]}")
            return ""
        lines = []
        for h in r.get("hits", []):
            if h.get("score", 0) < RECALL_MIN_SCORE:
                continue
            when = (h.get("recorded_at") or "")[:10]
            # The id is shown because merge_items needs it. Without it the
            # model can see that it recorded the same thing four times and
            # has no way to say which four.
            lines.append(f"- [{h.get('kind','?')} id={h.get('id')}] "
                         f"{h.get('text','')}" + (f"  ({when})" if when else ""))
        return "\n".join(lines)

    def _render(self, batch):
        """The one funnel: every segment becomes a line the model reads.

        Media is marked rather than dropped. Dropping it loses the thread
        whenever the user reacts to what is playing -- "huh, I should try that"
        is unrecordable without the line before it -- so the speech stays and
        the prompt says what may be done with it.
        """
        media = _media_names()
        lines = []
        for clip, segs, names in batch:
            for s in segs:
                spk = s.get("speaker")
                who = s.get("speaker_name") or (names.get(spk, {}) or {}).get("name") or spk or "UNKNOWN"
                tag = " [MEDIA]" if who in media else ""
                lines.append(f"{who}{tag}: {s['text'].strip()}")
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
        known = self._recall(text)
        if len(text) < MIN_CHARS:
            return {"clips": clips, "actions": 0, "said": "", "at": time.time(),
                    "skipped_reason": f"only {len(text)} characters of speech; "
                                      f"{MIN_CHARS} needed"}
        if len(text) > MAX_CHARS:
            text = text[-MAX_CHARS:]
            text = text[text.index("\n") + 1:] if "\n" in text else text

        REGISTRY  # noqa: B018 -- imported above; kept for clarity of intent
        try:
            import tools_impl
            tools_impl.set_context(clips)
        except Exception:
            pass
        self.busy = f"{len(clips)} clip(s)"
        self.notify("agent", status="running", clips=len(clips), chars=len(text))

        user = ""
        if known:
            user += "ALREADY KNOWN, from earlier conversations:\n" + known + "\n\n"
        user += f"Conversation transcript:\n\n{text}"
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user}]
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
            try:
                import tools_impl
                tools_impl.set_context([])
            except Exception:
                pass

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
    with _store_lock():
        _backfill_ids_locked(p)


def _backfill_ids_locked(p):
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
    with _store_lock():
        return _delete_item_locked(p, item_id)


def _delete_item_locked(p, item_id):
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
            # Out of memory too, or the agent keeps recalling something the
            # user deleted precisely because they did not want it kept.
            try:
                import semantic
                semantic.remove_item(item_id)
            except Exception:
                pass
            continue
        rows.append(d)
    if removed:
        atomicio.write_text(p, "".join(json.dumps(d) + "\n" for d in rows))
    return removed


def clear_items(kind=None):
    kinds = _kinds(kind)
    n = 0
    with _store_lock():
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
