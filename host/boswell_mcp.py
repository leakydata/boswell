#!/usr/bin/env python3
"""
Boswell over MCP: let an outside model read the archive.

The local agent writes notes as conversations end. This is the other way in --
a model you are talking to can search the recordings itself, pull a whole
conversation, and see who is in it, instead of being handed whatever the
extraction pass happened to save.

Two transports from the same definitions:

    uv run host/boswell_mcp.py                 # stdio, for a local client
    uv run host/boswell_mcp.py --http --port 8765   # for a remote one

stdio is the one to use for a client running on this machine: nothing listens
on a port and nothing is exposed. Register it with Claude Code as

    claude mcp add boswell -- uv run --directory <repo> host/boswell_mcp.py

--http exists because remote clients (ChatGPT connectors among them) cannot
speak stdio and need a URL. Read the warning on _require_http_ack before using
it: the archive is continuous recordings of real conversations, most of them
involving people who never agreed to be transcribed, let alone uploaded. Over
stdio the audio never leaves the machine. Over HTTP it goes wherever the client
is, and there is no taking it back.

It is no longer read-only, and the line that used to say so outlived the fact
by some months. Everything Boswell does can be reached from here: searching and
reading, recording what a conversation was worth, naming a voice or marking it
as something off a screen, checking why a recorder is not recording, clearing a
transcription backlog, rebuilding an index, running the reviewing pass, and
deleting a clip.

What is deliberately *not* here is anything that starts recording. There is no
tool to connect, arm, or unmute a device. A disarmed recorder is almost always
a decision somebody made about the room they are in, and a model should not be
able to reverse that decision on its own -- so `diagnose_recorder` will tell
you the radio is off and will not turn it on.

The same tools are a shell command each, through host/boswell_cli.py, which
reflects over the registry below rather than restating it.
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WEB = os.path.join(ROOT, "web")
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, WEB)

from mcp.server.mcpserver import MCPServer          # noqa: E402

server = MCPServer(
    name="boswell",
    title="Boswell recordings",
    version="1.0.0",
    instructions=(
        "A personal always-on audio archive: continuous 30-second clips, "
        "transcribed and diarized, grouped into conversations. Search it "
        "before asking the user to repeat something they have already said. "
        "Speaker labels are only as good as the voiceprints behind them -- "
        "check `identified` on a conversation before attributing a quote, and "
        "prefer quoting a conversation over a single clip, because a clip is a "
        "transport unit and usually cuts mid-sentence. You can also write "
        "to it: work through `unreviewed_conversations`, record what is "
        "worth keeping with record_fact / record_task / record_event / "
        "record_note, label it with tag_conversation, then mark_reviewed. "
        "Always pass the clips an item came from. Speech by anyone whose "
        "kind is media in list_people was audio playing nearby -- never "
        "record it as something the user said, planned or committed to. "
        "A speaker shown as SPEAKER_xx has not been identified: that label "
        "means a different voice in every recording, so it is never the "
        "subject of a fact -- and it is where the media filter leaks, "
        "because that filter works by name and an un-named podcast host "
        "arrives looking like somebody in the room. When you meet one, "
        "set_voice_kind('media') or name_voice fixes it for every later "
        "review too. The wearer talks at the screen while videos play, so "
        "his words and a video's are interleaved in the same clips and the "
        "video usually does most of the talking -- never read \"mostly "
        "video\" as \"nothing here\", and judge each line by who said it "
        "rather than the clip by its mix. Prefer search_units over search: a transcript line is "
        "whatever fell inside one 30-second clip, median seven words, while "
        "a unit is the sentence with the clip boundary undone. list_marks "
        "is where the user pressed the button on purpose, which is worth "
        "more attention than anything you find by searching."
    ),
)


def _fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


def _when(clip, ts=None):
    """When a clip was recorded, asked of the index when the caller has no
    timestamp to hand. Results that arrived by meaning rather than by keyword
    carry no `modified`, and "?" for the date of everything it found is the
    difference between a usable answer and a list of quotes."""
    if ts:
        return _fmt_time(ts)
    if not clip:
        return "?"
    try:
        import index_db
        r = index_db._conn().execute(
            "SELECT COALESCE(started, modified) t FROM clips WHERE name=?",
            (clip,)).fetchone()
        return _fmt_time(r["t"] if r else None)
    except Exception:
        return "?"


@server.tool(description="Overview of the archive: how many recordings, over "
                         "what period, how much is transcribed.")
def stats() -> dict:
    import index_db
    s = index_db.stats()
    import speaker_store
    c = speaker_store._conn()
    try:
        named = [p for p in speaker_store.people(c) if p["name"]]
        unknown = speaker_store.unknown_clusters(c)
    finally:
        c.close()
    return {
        "clips": s,
        "people_named": [p["name"] for p in named],
        "unidentified_voices": len(unknown),
        "note": ("Unidentified voices are recurring speakers nobody has put a "
                 "name to yet. Their speech is transcribed and searchable; it "
                 "is only the attribution that is missing."),
    }


@server.tool(description="Search everything said, by keyword. Returns matching "
                         "lines with their clip, time and speaker.")
def search(query: str, limit: int = 30) -> list:
    import index_db
    # index_db.search groups its hits by clip: one entry per clip, with the
    # matching lines under "hits". Flatten it -- a model wants the lines.
    out = []
    for clip in index_db.search(query, limit=limit):
        for h in clip.get("hits", []):
            out.append({"clip": clip.get("name"),
                        "when": _fmt_time(clip.get("modified")),
                        "at": round(h.get("start") or 0, 1),
                        "speaker": _resolve(clip.get("name"), h.get("speaker")),
                        "text": (h.get("snippet") or "").replace("<mark>", "")
                                                        .replace("</mark>", "")})
            if len(out) >= limit:
                return out
    return out


@server.tool(description="Search by meaning rather than wording, for when you "
                         "do not know the words that were used. Slower than "
                         "search and needs the local embedding model running.")
def search_by_meaning(query: str, limit: int = 25) -> list:
    import index_db
    import semantic
    keyword = index_db.search(query, limit=200)
    try:
        fused = semantic.hybrid(query, keyword, limit=limit)
    except Exception as e:
        return [{"error": f"semantic search unavailable: {e}",
                 "hint": "keyword search via `search` still works"}]
    # `hybrid` answers {"hits": [...], "error": ...} and each hit is a *clip*
    # with its matching lines nested under "hits" -- the same shape `search`
    # flattens. This read it as a flat list of lines, so it iterated the dict,
    # got the string "hits", and called .get() on it: the tool raised for
    # every query it was ever given. It failed as an exception out of the tool
    # rather than as a wrong answer, which is why nothing here caught it.
    if isinstance(fused, dict):
        if fused.get("error") and not fused.get("hits"):
            return [{"error": str(fused["error"]),
                     "hint": "keyword search via `search` still works"}]
        fused = fused.get("hits") or []
    out = []
    for clip in fused:
        name = clip.get("clip") or clip.get("name")
        for h in clip.get("hits") or [clip]:
            out.append({"clip": name, "when": _when(name, clip.get("modified")),
                        "at": round(h.get("start") or 0, 1),
                        "speaker": _resolve(name, h.get("speaker")),
                        "text": (h.get("text") or h.get("snippet") or "")
                                .replace("<mark>", "").replace("</mark>", ""),
                        "score": clip.get("score"),
                        "found_by": clip.get("found_by")})
            if len(out) >= limit:
                return out
    return out


# How far either side of a clip to look for the conversation it belongs to.
#
# Grouping the whole archive to answer "what conversation is this clip in" is
# the wrong shape of query and gets slower every day. A window is bounded, and
# it only has to be wide enough that no conversation reaches both edges: the
# longest in this archive at the shared gap is 145 minutes.
CONV_WINDOW = 12 * 3600


def _clip_time(clip):
    import index_db
    r = index_db._conn().execute(
        "SELECT COALESCE(started, modified) t FROM clips WHERE name=?",
        (clip,)).fetchone()
    return r["t"] if r else None


def _conversation_of(clip):
    """The conversation containing one clip, found by time rather than budget.

    This used to group "the last 400 clips" and look for the clip in there.
    400 clips is about three hours on a recorder that never stops, so a
    conversation from this afternoon -- the most useful material of the day --
    answered `no conversation contains omi_...`, while get_clip on the same
    name returned its transcript in full. The data was never missing; the
    grouping could not reach it.
    """
    import index_db
    at = _clip_time(clip)
    if at is None:
        return None
    for cv in index_db.conversations(index_db.CONVERSATION_GAP, SCAN_CLIPS,
                                     since=at - CONV_WINDOW,
                                     until=at + CONV_WINDOW):
        if clip in (cv.get("clips") or []):
            return cv
    return None


def _speaker_coverage(clips, sample=None):
    """How much of a conversation is attributed to a name, in segments.

    A boolean `identified` was worse than nothing. It went true the moment any
    one line carried a name, and the field the server instructions tell a model
    to check before attributing a quote read as solved on a block where 200
    segments of 2,191 were named -- 9.1%. It was also computed from the first
    40 clips of a 367-clip conversation, so it was 11% of the evidence
    describing all of it.
    """
    named = unnamed = 0
    who = set()
    for name in (clips[:sample] if sample else clips):
        t = _load_transcript(name)
        if not t:
            continue
        table = t.get("speakers") or {}
        for seg in t.get("segments") or []:
            spk = seg.get("speaker")
            label = seg.get("speaker_name") or (table.get(spk) or {}).get("name")
            if label:
                named += 1
                who.add(label)
            else:
                unnamed += 1
    total = named + unnamed
    return {"named_segments": named, "unnamed_segments": unnamed,
            "identified_fraction": round(named / total, 3) if total else None,
            "speakers": sorted(who)}


@server.tool(description="Recent conversations, newest first: when each one "
                         "was, how long, how many clips, and how much of it is "
                         "attributed to a name. `limit` counts conversations. "
                         "`identified_fraction` is the share of segments "
                         "carrying a name -- check it before quoting anyone, "
                         "because most of this archive is unnamed voices.")
def list_conversations(limit: int = 20) -> list:
    import index_db
    convs = index_db.conversations(index_db.CONVERSATION_GAP, SCAN_CLIPS)
    out = []
    for cv in convs[:limit]:
        clips = cv.get("clips") or []
        out.append({
            "start": _fmt_time(cv.get("start")),
            "end": _fmt_time(cv.get("end")),
            "minutes": round((cv.get("end", 0) - cv.get("start", 0)) / 60, 1),
            "clips": len(clips),
            "first_clip": clips[0] if clips else None,
            **_speaker_coverage(clips),
        })
    return out


def _load_transcript(clip):
    p = os.path.join(DATA, "transcripts", clip.rsplit(".", 1)[0] + ".json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def _resolve(clip, speaker):
    """Turn a per-clip diarizer label into a name, where one is known.

    SPEAKER_00 means nothing to a reader and nothing across clips, so handing
    it out raw invites a model to treat two unrelated voices as one person.
    """
    if not speaker or not clip:
        return speaker
    t = _load_transcript(clip)
    if not t:
        return speaker
    return ((t.get("speakers") or {}).get(speaker) or {}).get("name") or speaker


def _safe(clip):
    if not clip or os.path.basename(clip) != clip:
        raise ValueError(f"bad clip name: {clip!r}")
    return clip


@server.tool(description="The full text of one conversation, in order, with "
                         "speaker labels. Give it any clip name from that "
                         "conversation -- list_conversations returns one.")
def get_conversation(clip: str, max_chars: int = 40000) -> dict:
    _safe(clip)
    match = _conversation_of(clip)
    if match is None:
        return {"error": f"no conversation contains {clip}",
                "hint": "get_clip works on any clip name; this means the clip "
                        "is not in the index, not that it has no transcript"}

    clips = match.get("clips") or []
    head = {
        "start": _fmt_time(match.get("start")),
        "minutes": round((match.get("end", 0) - match.get("start", 0)) / 60, 1),
        "clips": len(clips),
        **_speaker_coverage(clips),
    }

    # Sections, because the flat wall was the failure `threads` was written to
    # prevent and it was never wired to this. One evening came back as 2,026
    # lines over 193 minutes covering dogs, a trade show, a commentary channel,
    # robotics tutorials and two AI videos, with nothing marking where one
    # ended -- while threads.sections() found 28 clean breaks in the same
    # material. It was computed and discarded.
    try:
        import threads
        built = threads.for_conversation(clips)
        secs = built.get("sections") or []
    except Exception as e:
        secs = []
        head["sections_unavailable"] = str(e)[:120]

    if secs:
        out, used, truncated = [], 0, False
        for i, sec in enumerate(secs):
            body = []
            for u in sec["units"]:
                who = u.get("name") or u.get("speaker") or "?"
                body.append(f"{who}: {(u.get('text') or '').strip()}")
            text = "\n".join(body)
            if used + len(text) > max_chars:
                truncated = True
                break
            used += len(text)
            out.append({"section": i, "at": _fmt_time(sec["units"][0].get("at"))
                        if sec.get("units") else None,
                        "units": len(sec["units"]), "text": text})
        return dict(head, sections=out, sections_total=len(secs),
                    truncated=truncated)

    # No sections: a short conversation, or the embedder is down. The flat
    # form is still the right answer for something that is genuinely one
    # stretch of talk.
    lines, truncated = [], False
    for name in clips:
        t = _load_transcript(name)
        if not t:
            continue
        who = {k: (v or {}).get("name") for k, v in (t.get("speakers") or {}).items()}
        for seg in (t.get("segments") or []):
            label = who.get(seg.get("speaker")) or seg.get("speaker") or "?"
            lines.append(f"{label}: {seg['text']}")
            if sum(len(x) for x in lines) > max_chars:
                truncated = True
                break
        if truncated:
            break
    return dict(head, truncated=truncated, text="\n".join(lines))


@server.tool(description="One clip's transcript, with timings. Usually you "
                         "want get_conversation instead -- a clip is a 30-second "
                         "transport unit and normally cuts mid-sentence.")
def get_clip(clip: str) -> dict:
    _safe(clip)
    t = _load_transcript(clip)
    if not t:
        return {"error": f"no transcript for {clip}"}
    who = {k: (v or {}).get("name") for k, v in (t.get("speakers") or {}).items()}
    out = {
        "clip": clip,
        "when": _fmt_time(t.get("created")),
        "segments": [{"start": s.get("start"), "end": s.get("end"),
                      "speaker": who.get(s.get("speaker")) or s.get("speaker"),
                      "text": s.get("text")} for s in (t.get("segments") or [])],
    }
    # A reader that cannot tell these apart will quote guessed words as though
    # they were said. The words are not invented -- there is real speech under
    # them -- but only the loud ones survive the distance intact.
    if t.get("unattributed"):
        out["caveat"] = ("A voice was found in this clip but no line could be "
                         "attributed to it, which is what speech at a distance "
                         "looks like -- a television, or someone talking in "
                         "another room. Treat prominent words as real and the "
                         "sentences around them as approximate. Do not quote "
                         "this as verbatim speech.")
    if t.get("sounds"):
        # name, score, and where in the clip it was loudest -- the last of
        # those is what lets a reader say "a dog barked about ten seconds in"
        # rather than "a dog barked at some point in these thirty seconds".
        import index_db as _idx
        out["sounds"] = [{"sound": r[0], "score": r[1],
                          # Some classes are reliably right about the sound and
                          # wrong about the object. Say so here rather than let
                          # a reader conclude there is a typewriter.
                          **({"really": _idx.SOUND_ALIASES[r[0]]}
                             if r[0] in _idx.SOUND_ALIASES else {}),
                          **({"at_seconds": r[2]} if len(r) > 2 else {})}
                         for r in t["sounds"][:6] if r]
    return out


@server.tool(description="Who the system can recognise by voice, and how many "
                         "voiceprints back each of them.")
def list_people() -> list:
    # The profile travels with the name. A reader that gets "Danny Polishchuk"
    # and nothing else cannot tell a housemate from a comedian on a channel
    # playing in the background, and the difference changes what the words
    # mean.
    import speaker_store
    c = speaker_store._conn()
    try:
        out = []
        for p in speaker_store.people(c):
            if not p["name"]:
                continue
            row = {"name": p["name"], "voiceprints": p["prints"],
                   "speech_seconds": round(p["seconds"] or 0, 1)}
            if p.get("kind"):
                row["kind"] = p["kind"]
            if p.get("role"):
                row["role"] = p["role"]
            if p.get("note"):
                row["note"] = p["note"]
            out.append(row)
        return out
    finally:
        c.close()


@server.tool(description="Recurring voices nobody has named, largest first. "
                         "Useful for telling the user who is worth identifying "
                         "next, and what they talked about.")
def unidentified_voices(limit: int = 10) -> list:
    import pipeline
    out = []
    import speaker_store
    for v in pipeline.labelling_queue(limit=limit):
        cands = [{"name": c["name"], "score": c["score"]}
                 for c in v["candidates"]]
        row = {
            "id": v["person_id"],
            "minutes": round(v["seconds"] / 60, 1),
            "clips": v["clips"],
            "closest_named": cands,
            "said": v["text"][:400],
        }
        if not cands:
            # An empty list reads as "there are no named people to compare
            # against", which is false and invites a guess. Measured on this
            # archive, every one of the eight voices in the queue had its top
            # three shown at 0.35-0.47 against a different-people p99 of
            # 0.572: an ordering of noise, presented as candidates.
            row["no_candidate"] = (
                f"nothing scores at or above {speaker_store.MATCH_LOW}, which "
                f"is where a comparison starts meaning anything on this "
                f"archive. Identify this voice from what it says, not from a "
                f"ranking.")
        out.append(row)
    return out


@server.tool(description="What the local agent recorded from conversations: "
                         "tasks, events, notes, facts or topics.")
def recorded_items(kind: str = "notes", limit: int = 50) -> list:
    import agent_runner
    if kind not in agent_runner.KINDS:
        return [{"error": f"kind must be one of {list(agent_runner.KINDS)}"}]
    return [_trim_clips(i) for i in agent_runner.load_items(kind, limit=limit)]


# How many clip names an item shows before they are counted instead.
#
# Provenance is required on every write and a widened conversation can run to
# three hundred clips, so a list of ten tasks came back as two thousand file
# names around the sentences that mattered. Unreadable on a terminal and
# expensive in a context window, for a list nobody reads -- what is wanted is
# where it came from, and `get_conversation` on the first clip answers that.
SHOW_CLIPS = 4


def _trim_clips(item):
    clips = item.get("_clips") or []
    if len(clips) <= SHOW_CLIPS:
        return item
    out = dict(item)
    out["_clips"] = clips[:SHOW_CLIPS]
    out["_clips_total"] = len(clips)
    out["_conversation"] = clips[0]
    return out


# ---------------------------------------------------------------- writing
#
# The archive used to be read-only here, and everything below was done instead
# by a 20B model running locally against the same transcripts. It was the weak
# link: it recorded a video host's claims as durable facts about the user, wrote
# "SPEAKER_00 has a COO, CCO, CFO" as a fact about a person, and recorded the
# same sentence four times because it could not tell it had written it before.
# The tools it used were fine; the judgement was not. So the tools are exposed
# here instead, for a model that can weigh who is speaking and whether a
# sentence is worth keeping at all.

SAID_BY_NOTE = (' `said_by` is the name on the transcript line the words came from -- required, because this archive has video playing near the microphone and often in the same clips as somebody talking back at it, so who said a thing is not recoverable from the clip it is in.')

_ctx = __import__("threading").Lock()


def _write(fn_name, clips, **kw):
    """Call a tools_impl writer with provenance attached.

    tools_impl carries the clip list in a module global that the local agent
    sets once per batch. There are no batches here -- each call arrives on its
    own -- so the caller passes clips explicitly and they are installed around
    the one call. The lock is because that global is shared: two writes racing
    would otherwise stamp each other's provenance onto the wrong record.

    Provenance is required rather than optional. An item with no clips cannot
    be traced back to what was actually said, which is how the store ended up
    with 74 facts nobody could check.
    """
    import tools_impl
    if isinstance(clips, str):
        clips = [clips]
    try:
        # A structured error, not a traceback: the caller is a model, and an
        # exception out of a tool tells it nothing it can act on.
        clips = [_safe(c) for c in (clips or []) if c]
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not clips:
        return {"ok": False, "error": "clips is required -- pass the clip "
                                      "name(s) this came from, so the item "
                                      "can be traced back to the audio"}
    missing = [c for c in clips if _load_transcript(c) is None]
    if missing:
        return {"ok": False, "error": f"no transcript for {missing[:3]} -- "
                                      f"use a clip name from list_conversations"}
    with _ctx:
        tools_impl.set_context(clips)
        try:
            return tools_impl.REGISTRY[fn_name](**kw)
        finally:
            tools_impl.set_context([])


@server.tool(description="Record a durable fact about a person or project. "
                         "Pass the clip name(s) it came from." + SAID_BY_NOTE)
def record_fact(subject: str, fact: str, said_by: str, clips: list) -> dict:
    return _write("remember_fact", clips, subject=subject, fact=fact,
                  said_by=said_by)


@server.tool(description="Record an action item someone committed to. `due` is "
                         "free text or a date. Pass the clip name(s) it came from.")
def record_task(text: str, said_by: str, clips: list, due: str = None,
                owner: str = None) -> dict:
    return _write("add_task", clips, text=text, due=due, owner=owner,
                  said_by=said_by)


@server.tool(description="Record a meeting or deadline mentioned in "
                         "conversation. Pass the clip name(s) it came from." + SAID_BY_NOTE)
def record_event(title: str, start: str, said_by: str, clips: list,
                 end: str = None, attendees: list = None) -> dict:
    return _write("add_calendar_event", clips, title=title, start=start,
                  end=end, attendees=attendees or [], said_by=said_by)


@server.tool(description="Record context worth keeping that is not a fact, "
                         "task or event. Pass the clip name(s) it came from." + SAID_BY_NOTE)
def record_note(title: str, body: str, said_by: str, clips: list,
                tags: list = None) -> dict:
    return _write("add_note", clips, title=title, body=body,
                  tags=tags or [], said_by=said_by)


@server.tool(description="Save what a video, podcast or stream said -- the "
                         "media lane. Use it for anything a voice marked "
                         "media said, and for an unnamed voice plainly "
                         "addressing an audience rather than the room. "
                         "Nothing recorded here becomes a fact, task or event "
                         "about anybody, so it is the right place for the "
                         "content the wearer is watching rather than "
                         "something to be suppressed. `source` is the channel "
                         "or presenter if the transcript names one.")
def record_media_note(title: str, body: str, clips: list,
                      source: str = None, tags: list = None) -> dict:
    return _write("add_media_note", clips, title=title, body=body,
                  source=source, tags=tags or [])


@server.tool(description="Label a conversation with the subjects it covered, "
                         "so later conversations on the same subject can be "
                         "found with it. Short plain labels, not sentences.")
def tag_conversation(clips: list, topics: list) -> dict:
    return _write("tag_topics", clips, topics=topics)


@server.tool(description="Fold duplicate recorded items into one. The survivor "
                         "keeps its id, gains the others' clips, and may have "
                         "its wording replaced.")
def merge_recorded(kind: str, keep_id: str, drop_ids: list,
                   text: str = None) -> dict:
    import agent_runner, tools_impl
    if kind not in agent_runner.KINDS:
        return {"ok": False, "error": f"kind must be one of {list(agent_runner.KINDS)}"}
    if isinstance(drop_ids, str):
        drop_ids = [drop_ids]
    return tools_impl.merge_items(kind, keep_id, drop_ids, text=text)


@server.tool(description="Delete one recorded item by id -- something that was "
                         "never worth recording, or came from audio playing "
                         "nearby rather than from the user.")
def delete_recorded(kind: str, item_id: str) -> dict:
    import agent_runner
    if kind not in agent_runner.KINDS:
        return {"ok": False, "error": f"kind must be one of {list(agent_runner.KINDS)}"}
    ok = agent_runner.delete_item(kind, item_id)
    return {"ok": bool(ok), "deleted": item_id if ok else None,
            "error": None if ok else "no item with that id"}


# How far back the review queue looks, in clips.
#
# It was 400, which is the last two or three hours on a recorder that never
# stops -- so a queue meant for working through the archive only ever showed
# today, and a clip from this afternoon was already outside it. The whole
# archive at 6,000 clips groups in under a second, and the queue is not on any
# hot path.
SCAN_CLIPS = 20000

# How much of a conversation must already be read for it to leave the queue.
# Below this it comes back: a conversation that has grown since it was reviewed
# is genuinely new speech, and re-reading the whole thing is cheap next to
# missing the part that was added.
REVIEWED_SHARE = 0.6


@server.tool(description="Conversations nobody has reviewed yet, oldest first. "
                         "This is the queue to work through; mark_reviewed "
                         "takes one off it.")
def unreviewed_conversations(limit: int = 20) -> list:
    import agent_runner, index_db
    done = agent_runner.reviewed_clips()
    out = []
    for conv in reversed(index_db.conversations(index_db.CONVERSATION_GAP,
                                                SCAN_CLIPS)):
        first = conv["clips"][0] if conv.get("clips") else None
        if not first:
            continue
        # Coverage, not identity. On a recorder that never stops, the gap that
        # separated two conversations fills in and yesterday's conversation is
        # today's middle -- so keying the queue on the first clip showed the
        # same speech again under a new name, forever. Anything still mostly
        # unread comes back; a conversation that has grown a tail since it was
        # read is worth re-reading.
        clips = set(conv["clips"])
        seen = len(clips & done)
        if seen >= REVIEWED_SHARE * len(clips):
            continue
        partly = seen or None
        out.append({
            "first_clip": first,
            "start": _fmt_time(conv.get("start")),
            "minutes": round((conv.get("seconds") or 0) / 60.0, 1),
            "clips": len(conv["clips"]),
            # Reviewing is per window, grouping is per gap, and the two are
            # not the same size -- so a long conversation is worked through
            # in pieces and the queue has to be able to say how far in it is.
            "already_reviewed": partly,
            # Resolved names, so the queue shows at a glance whether a
            # conversation is the user or a video that happened to be playing.
            "speakers": conv.get("speakers") or [],
            "preview": (conv.get("preview") or "")[:160],
        })
        if len(out) >= limit:
            break
    return out


@server.tool(description="Mark a conversation reviewed so it leaves the queue. "
                         "Call it after recording whatever was worth keeping -- "
                         "including when nothing was.")
def mark_reviewed(clip: str, note: str = None, clips: list = None) -> dict:
    import agent_runner
    try:
        clip = _safe(clip)
        covered = [_safe(c) for c in (clips or []) if c]
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    # Deliberately no guessing here. Resolving the clip to "its conversation"
    # and marking all of that was tried and retired 412 clips on the strength
    # of 79 having been read -- because the 300-second gap groups a whole
    # evening of a never-stopping recorder into one conversation. Pass the
    # clips the review actually covered; review_conversation returns them.
    r = agent_runner.mark_reviewed(clip, note=note, clips=covered or [clip])
    return {"ok": True, "clip": r["clip"], "clips_covered": len(r["clips"]),
            "at": r["at"], "note": r.get("note")}


# ---------------------------------------------------------------------------
# The half of Boswell that only the running server can reach.
#
# This process reads the same files the web server does, which is enough for
# searching and for everything the agent store holds. It is not enough for
# anything live: the transcription queue is an object inside the server, the
# Bluetooth link is a connection it owns, and the reviewing agent is a thread
# in it. Reaching those from here means asking that server, not reimplementing
# them -- a second process that loads Whisper to clear a backlog would fight
# the first one for the card and for the same files.


def _base():
    # Named in web/netcfg.py rather than repeated here: this process is
    # spawned by whatever is using the tools, so it never sees the systemd
    # unit's environment, and a default that drifts from the server's leaves
    # every tool reporting "connection refused" at a healthy server.
    import netcfg
    return netcfg.base_url()


def _api(path, body=None, method=None, timeout=120):
    """One call to the running server, or a structured reason it failed.

    A traceback tells a model nothing it can act on, and "connection refused"
    tells it nothing about which program is not running.
    """
    import urllib.error
    import urllib.request
    url = _base().rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    tok = os.environ.get("BOSWELL_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if data is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = (json.load(e) or {}).get("detail", "")
        except Exception:
            pass
        if e.code == 401:
            return {"ok": False, "error": "the server rejected the token",
                    "hint": "set BOSWELL_TOKEN to the same value the server "
                            "was started with"}
        return {"ok": False, "error": f"{e.code} from {path}"
                                      + (f": {str(detail)[:200]}" if detail else "")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:140]}",
                "hint": f"the Boswell server is not answering on {_base()}. "
                        f"Start it with `uv run web/server.py`, or set "
                        f"BOSWELL_URL if it is somewhere else."}


# ---- searching what was actually said -------------------------------------


@server.tool(description="Search by meaning over whole thoughts rather than "
                         "transcript lines, with filters. A line is whatever "
                         "fell inside one 30-second clip -- median seven words "
                         "-- so this is usually the better search of the two. "
                         "`person` is a speaker name, `sound` a situational "
                         "tag from list_sounds, `since`/`until` are "
                         "YYYY-MM-DD.")
def search_units(query: str, limit: int = 15, person: str = None,
                 sound: str = None, since: str = None,
                 until: str = None) -> list:
    import units

    def epoch(d):
        if not d:
            return None
        try:
            return time.mktime(time.strptime(d[:10], "%Y-%m-%d"))
        except ValueError:
            return None

    r = units.search(query, limit=limit, name=person, sound=sound,
                     since=epoch(since), until=epoch(until))
    if r.get("error"):
        return [{"error": r["error"],
                 "hint": "keyword search via `search` still works"}]
    out = []
    for h in r.get("hits", []):
        out.append({
            "clip": h["clip"], "when": _when(h["clip"], h.get("at")),
            "speaker": h.get("name") or h.get("speaker"),
            "words": h.get("words"), "sounds": h.get("sounds"),
            "score": h.get("score"), "text": h.get("text"),
        })
    if not out:
        return [{"error": "nothing matched",
                 "hint": "the unit index may be stale or empty -- "
                         "rebuild_index('units') builds it"}]
    return out


@server.tool(description="Presses on the recorder's button, with what was "
                         "said around each one. A single tap is a bookmark and "
                         "a double tap a reminder. These are the moments the "
                         "user deliberately flagged, so they are worth more "
                         "attention than anything found by search.")
def list_marks(limit: int = 20, kind: str = None) -> list:
    import marks
    out = []
    for m in marks.recent(limit=limit, kind=kind or None):
        clips = m.get("clips") or []
        out.append({"means": m.get("means"), "press": m.get("kind"),
                    "when": _fmt_time(m.get("at")),
                    # Every clip the surrounding speech was drawn from, because
                    # a mark is an instant and the talk around it crosses clip
                    # boundaries -- and record_* needs the whole list to be
                    # able to trace an item back.
                    "clips": clips, "first_clip": clips[0] if clips else None,
                    "text": (m.get("text") or "")[:1500]})
    return out or [{"note": "no button presses recorded"}]


@server.tool(description="Subjects the archive has been labelled with, "
                         "commonest first, and how many conversations carry "
                         "each. Use one as a query to `search` to find them.")
def list_topics(limit: int = 40) -> list:
    r = _api("/api/topics")
    if isinstance(r, dict) and r.get("ok") is False:
        return [r]
    out = []
    for t in (r.get("topics") or [])[:limit]:
        out.append({"topic": t.get("topic"), "conversations": t.get("count"),
                    "first_clip": (t.get("clips") or [None])[0]})
    return out


@server.tool(description="Situational sound tags the archive carries -- "
                         "typing, a dog, music -- usable as the `sound` filter "
                         "on search_units.")
def list_sounds(limit: int = 40) -> list:
    r = _api("/api/sounds")
    if isinstance(r, dict) and r.get("ok") is False:
        return [r]
    rows = r.get("sounds") if isinstance(r, dict) else r
    return (rows or [])[:limit]


# ---- who the voices are ---------------------------------------------------
#
# These matter more than they look. The agent refuses to record a fact about
# an unnamed voice, and the [MEDIA] filter that keeps a podcast host's claims
# out of the record works by name -- so an unidentified recurring voice is a
# hole in both. Naming one, or marking it media, closes it for every future
# review as well as this one.


@server.tool(description="Put a name to a recurring voice, from the person_id "
                         "in unidentified_voices. Every voiceprint gathered "
                         "under that cluster becomes a labelled reference. "
                         "`kind` is required and says what the voice is -- "
                         "'person' in the room, 'media' off a screen, or "
                         "'ignored'. Naming without it leaves a voice trusted "
                         "as a person by default, and most new voices in this "
                         "archive are videos.")
def name_voice(person_id: int, name: str, kind: str) -> dict:
    if not (name or "").strip():
        return {"ok": False, "error": "need a name"}
    if kind not in ("person", "media", "ignored"):
        return {"ok": False,
                "error": "kind must be 'person', 'media' or 'ignored'"}
    named = _api(f"/api/voices/{int(person_id)}/name", {"name": name.strip()})
    if isinstance(named, dict) and named.get("ok") is False:
        return named
    # Both halves or neither. A name applied while the kind write fails is
    # exactly the state this argument exists to prevent.
    classified = _api(f"/api/voices/{int(person_id)}/kind", {"kind": kind})
    if isinstance(classified, dict) and classified.get("ok") is False:
        return dict(classified, named=True,
                    error="named, but not classified: " +
                          str(classified.get("error")))
    return {"ok": True, "person_id": int(person_id), "name": name.strip(),
            "kind": kind}


@server.tool(description="How well each named voice's reference set agrees "
                         "with itself. A reference is a set of voiceprints "
                         "that are supposed to be one person; if two people "
                         "were merged into it, its pairs disagree and every "
                         "later match inherits the mistake. Compare `median` "
                         "against 0.863, which is what one person scores on "
                         "this archive.")
def voice_health(min_prints: int = 12) -> list:
    """Measured, not inferred. This exists because the question "is the
    wearer's reference drifting?" was being answered by argument -- he is
    37.8% of all segments and carries 588 voiceprints, so it mattered -- and
    the answer turned out to be no while two much smaller references were
    genuinely mixed.
    """
    import numpy as np
    import speaker_store as ss
    c = ss._conn()
    try:
        out = []
        for p in ss.people(c):
            if not p.get("name") or (p.get("prints") or 0) < min_prints:
                continue
            rows = c.execute(
                "SELECT vec FROM voiceprints WHERE person_id=? AND vec IS NOT NULL",
                (p["id"],)).fetchall()
            vecs = [ss._unpack(r["vec"]) for r in rows]
            vecs = [np.asarray(v, dtype=float) for v in vecs if v is not None]
            if len(vecs) < min_prints:
                continue
            M = np.stack(vecs)
            M = M / np.linalg.norm(M, axis=1, keepdims=True)
            pairs = (M @ M.T)[np.triu_indices(len(M), k=1)]
            median = float(np.median(pairs))
            out.append({
                "name": p["name"], "person_id": p["id"],
                "kind": p.get("kind") or "unclassified",
                "voiceprints": len(vecs),
                "median": round(median, 3),
                "p10": round(float(np.percentile(pairs, 10)), 3),
                "share_below_match_low": round(float(np.mean(pairs < ss.MATCH_LOW)), 3),
                # One person medians 0.863 here; different people median 0.107.
                # A reference sitting between the two holds more than one voice.
                "verdict": ("looks like one person" if median >= 0.75 else
                            "mixed -- probably more than one voice"
                            if median < 0.60 else "worth listening to"),
            })
    finally:
        c.close()
    out.sort(key=lambda r: r["median"])
    return out


@server.tool(description="Voices that have a name but have never been said to "
                         "be a person or a video. Nothing they say can be "
                         "recorded until that is settled, and the list grows "
                         "on its own because naming is automatic and "
                         "classifying is not -- so this is the queue that "
                         "keeps extraction working.")
def unclassified_voices() -> list:
    import speaker_store
    c = speaker_store._conn()
    try:
        rows = [{"person_id": p["id"], "name": p["name"],
                 "speech_seconds": round(p.get("seconds") or 0, 1),
                 "voiceprints": p.get("prints")}
                for p in speaker_store.people(c)
                if p.get("name") and not p.get("kind")]
    finally:
        c.close()
    rows.sort(key=lambda r: -(r["speech_seconds"] or 0))
    return rows or [{"note": "every named voice is classified"}]


@server.tool(description="Say whether a voice is a person in the room, audio "
                         "off a screen, or noise not worth recognising. kind "
                         "is 'person', 'media' or 'ignored'. Marking a voice "
                         "'media' stops anything it says being recorded as "
                         "something the user said or committed to -- do that "
                         "for a podcast host or a video, even without knowing "
                         "who they are.")
def set_voice_kind(person_id: int, kind: str) -> dict:
    if kind not in ("person", "media", "ignored"):
        return {"ok": False,
                "error": "kind must be 'person', 'media' or 'ignored'"}
    return _api(f"/api/voices/{int(person_id)}/kind", {"kind": kind})


# ---- is it actually recording, and is it transcribed? ---------------------


@server.tool(description="The recorders this install knows and whether each "
                         "is connected right now.")
def recorders() -> dict:
    r = _api("/api/recorders")
    if isinstance(r, dict) and r.get("ok") is False:
        return r
    return {"recorders": [
        {"name": d.get("name"), "kind": d.get("kind"), "id": d.get("id"),
         "address": d.get("address"), "connected": d.get("connected")}
        for d in (r.get("recorders") or [])]}


@server.tool(description="Why is a recorder not recording? Checks the radio, "
                         "the bond, and whether the device is advertising, and "
                         "says which of those is the problem. Read-only -- it "
                         "does not connect or disconnect anything.")
def diagnose_recorder(seconds: float = 8.0) -> dict:
    return _api("/api/recorders/diagnose", {}, timeout=max(30, seconds * 4))


@server.tool(description="How much of the archive is transcribed, and what is "
                         "queued or running now. A silent backlog here is the "
                         "usual reason a search finds nothing.")
def transcription_status() -> dict:
    import index_db
    q = _api("/api/queue")
    s = index_db.stats()
    out = {"clips": s.get("clips"), "with_speech": s.get("with_speech"),
           "segments": s.get("segments")}
    if isinstance(q, dict) and q.get("ok") is False:
        out["queue"] = q            # keep the reason, do not swallow it
    else:
        out.update(queued=q.get("pending"), running=q.get("busy"),
                   automatic=q.get("auto"))
    return out


@server.tool(description="Queue every clip that has no transcript yet. Returns "
                         "how many are waiting in total, not only what this "
                         "call added -- most of a backlog is usually already "
                         "in flight.")
def transcribe_missing() -> dict:
    return _api("/api/transcribe_all", {})


@server.tool(description="Rebuild a search index. 'units' is the meaning index "
                         "search_units uses and nothing rebuilds it on a "
                         "schedule, so it goes stale after new recordings; "
                         "'keyword' re-syncs the clip index; 'meaning' embeds "
                         "any transcript line not yet indexed.")
def rebuild_index(which: str = "units") -> dict:
    if which == "units":
        import units
        n = units.rebuild()
        return {"ok": True, "index": "units", "units": n}
    if which == "keyword":
        return {"ok": True, "index": "keyword", **(_api("/api/index/rebuild", {}) or {})}
    if which == "meaning":
        return {"ok": True, "index": "meaning",
                **(_api("/api/search/semantic/rebuild", {}, timeout=1800) or {})}
    return {"ok": False,
            "error": "which must be 'units', 'keyword' or 'meaning'"}


# ---- the reviewing pass ---------------------------------------------------


@server.tool(description="Run the reviewing agent over one conversation now, "
                         "rather than waiting for it to fire on silence. Pass "
                         "the clip names; it widens them to the whole "
                         "conversation itself. This is the same pass that "
                         "writes tasks, facts, events and topic tags.")
def review_conversation(clips: list) -> dict:
    if isinstance(clips, str):
        clips = [clips]
    try:
        clips = [_safe(c) for c in (clips or []) if c]
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not clips:
        return {"ok": False, "error": "need at least one clip name"}
    r = _api("/api/agent/review", {"names": clips}, timeout=900)
    if isinstance(r, dict) and isinstance(r.get("clips"), list):
        # The list stays, because it is the argument to mark_reviewed: what
        # the agent widened to is exactly what has now been read, and it is
        # not the same thing as "the conversation" -- grouping is much
        # coarser than one review's window.
        r = dict(r, clips_read=len(r["clips"]),
                 conversation=r["clips"][0] if r["clips"] else None)
    return r


@server.tool(description="What the reviewing agent is set to and whether it "
                         "can reach its model: which backend, which model, "
                         "and how much is waiting to be read.")
def agent_status() -> dict:
    r = _api("/api/agent")
    if isinstance(r, dict) and r.get("ok") is False:
        return r
    out = {k: r.get(k) for k in
           ("enabled", "backend", "model", "backend_ready",
            "pending_clips", "pending_chars", "busy")}
    # A backend and a model from different worlds is this project's favourite
    # kind of fault: nothing is broken until the next conversation ends, and
    # then it fails in a log nobody reads. Found in exactly that state --
    # backend `openrouter`, model `gpt-oss:20b`, which is an Ollama tag no
    # hosted provider serves.
    import llm
    model = out.get("model") or ""
    if out.get("backend") == "local" and "/" in model:
        out["warning"] = f"{model!r} is a hosted model name and the backend " \
                         f"is Ollama on this machine"
    elif out.get("backend") in ("openai", "openrouter") and "/" not in model:
        out["warning"] = f"{model!r} looks like an Ollama tag, not a model " \
                         f"{out['backend']} serves -- reviews will fail"
    elif out.get("backend") == "anthropic" and model not in llm.CLAUDE_MODELS:
        out["warning"] = f"{model!r} is not one of {list(llm.CLAUDE_MODELS)}"
    if out.get("enabled") is False and out.get("pending_clips"):
        out["note"] = (f"{out['pending_clips']} clip(s) are waiting and "
                       f"automatic reviewing is off; review_conversation "
                       f"still works on demand")
    return out


@server.tool(description="Delete clips and their transcripts -- a recording "
                         "that is silence, a test, or audio that should never "
                         "have been kept. Voiceprints are untouched. Names "
                         "must be given in full; there is no pattern form, on "
                         "purpose.")
def delete_clips(clips: list) -> dict:
    if isinstance(clips, str):
        clips = [clips]
    try:
        clips = [_safe(c) for c in (clips or []) if c]
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not clips:
        return {"ok": False, "error": "need at least one clip name"}
    return _api("/api/clips/delete", {"names": clips})


def _require_http_ack():
    """Make exposing the archive over a port a deliberate act.

    stdio keeps everything on this machine. A port does not: whatever connects
    can read every conversation in the archive, and those conversations involve
    people who were recorded without being asked and cannot un-share what goes
    out. That is a decision worth typing a flag for rather than discovering
    afterwards, so --http alone is not enough.
    """
    if os.environ.get("BOSWELL_MCP_ALLOW_HTTP") == "1":
        return
    sys.exit(
        "Refusing to serve the archive over HTTP without an explicit "
        "acknowledgement.\n\n"
        "  Over stdio the recordings never leave this machine. Over HTTP they "
        "go wherever\n  the client is, including a third-party service if that "
        "is what connects -- and the\n  archive is continuous recordings of "
        "other people who did not agree to that.\n\n"
        "  If that is what you want:  BOSWELL_MCP_ALLOW_HTTP=1 "
        "uv run host/boswell_mcp.py --http\n"
        "  Bind it to loopback and put a tunnel with its own auth in front; "
        "this server has none."
    )


def _bearer_gate(app, token):
    """Refuse anything without the token, before it reaches the archive.

    Defence in depth, not the main lock. The main lock is that this binds to
    loopback or to a tailnet address and is never public. But a tunnel
    misconfigured, an access policy that lapses, or five minutes of
    `--host 0.0.0.0` while testing something should not be the only thing
    between a stranger and every conversation in the house. The tunnel
    carrying the auth and the server carrying none is one mistake deep.

    Compared with compare_digest so a wrong token cannot be found a character
    at a time by timing the refusal.
    """
    import hmac

    expected = f"Bearer {token}"

    async def gated(scope, receive, send):
        if scope.get("type") != "http":
            return await app(scope, receive, send)
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        got = headers.get(b"authorization", b"").decode("utf-8", "replace")
        if not hmac.compare_digest(got, expected):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"text/plain"),
                                    (b"www-authenticate", b'Bearer realm="boswell"')]})
            await send({"type": "http.response.body",
                        "body": b"boswell: a bearer token is required\n"})
            return
        await app(scope, receive, send)

    return gated


def _http_token():
    """The token HTTP mode requires, or exit saying how to set one.

    Required rather than optional. An unauthenticated port is fine on
    loopback and catastrophic the moment anything forwards to it, and the
    difference between those two is a decision made elsewhere, later, by
    somebody who may not remember this ran without a password.
    """
    token = os.environ.get("BOSWELL_MCP_TOKEN", "")
    if len(token) >= 16:
        return token
    sys.exit(
        "Refusing to serve the archive over HTTP without a token.\n\n"
        "  Set one, at least 16 characters:\n"
        "      export BOSWELL_MCP_TOKEN=\"$(openssl rand -hex 24)\"\n\n"
        "  Clients send it as:  Authorization: Bearer <token>\n\n"
        "  This is not the main protection -- bind to loopback or a tailnet\n"
        "  address and keep the port off the public internet. It is what\n"
        "  stands there when that goes wrong.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http", action="store_true",
                    help="serve over streamable HTTP instead of stdio, for "
                         "clients that cannot speak stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.http:
        _require_http_ack()
        token = _http_token()
        # host and port go to uvicorn, not to server.settings: mcp 2.x
        # dropped those fields, and setting them raised
        # `"Settings" object has no field "host"` -- so HTTP mode had been
        # broken since that upgrade and nobody had run it to find out.
        import uvicorn
        print(f"boswell mcp on http://{a.host}:{a.port}/mcp  (bearer token required)",
              file=sys.stderr, flush=True)
        uvicorn.run(_bearer_gate(server.streamable_http_app(), token),
                    host=a.host, port=a.port, log_level="warning")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
