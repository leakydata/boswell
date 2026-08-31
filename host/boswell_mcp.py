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

Read-only by design. There is no tool here that writes, renames, deletes, or
enrols anything. A model summarising your week has no business editing what it
is summarising, and a mistake it makes should not be able to change the record.
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
        "transport unit and usually cuts mid-sentence."
    ),
)


def _fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


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
        hits = semantic.hybrid(query, keyword, limit=limit)
    except Exception as e:
        return [{"error": f"semantic search unavailable: {e}",
                 "hint": "keyword search via `search` still works"}]
    out = []
    for h in hits:
        clip = h.get("clip") or h.get("name")
        out.append({"clip": clip, "when": _fmt_time(h.get("modified")),
                    "at": round(h.get("start") or 0, 1),
                    "speaker": _resolve(clip, h.get("speaker")),
                    "text": h.get("text") or h.get("snippet"),
                    "score": h.get("score")})
    return out


@server.tool(description="Recent conversations, newest first: when each one "
                         "was, how long, how many clips, and who is in it.")
def list_conversations(limit: int = 20) -> list:
    import index_db
    convs = index_db.conversations(gap_seconds=300, limit=400)
    out = []
    for cv in convs[:limit]:
        speakers = set()
        identified = False
        for name in cv.get("clips", [])[:40]:
            t = _load_transcript(name)
            if not t:
                continue
            for spk, info in (t.get("speakers") or {}).items():
                who = (info or {}).get("name")
                if who:
                    speakers.add(who)
                    identified = True
        out.append({
            "start": _fmt_time(cv.get("start")),
            "end": _fmt_time(cv.get("end")),
            "minutes": round((cv.get("end", 0) - cv.get("start", 0)) / 60, 1),
            "clips": len(cv.get("clips", [])),
            "first_clip": (cv.get("clips") or [None])[0],
            "speakers": sorted(speakers),
            "identified": identified,
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
    import index_db
    _safe(clip)
    convs = index_db.conversations(gap_seconds=300, limit=400)
    match = next((cv for cv in convs if clip in (cv.get("clips") or [])), None)
    if match is None:
        return {"error": f"no conversation contains {clip}"}

    lines, names, truncated = [], set(), False
    for name in match["clips"]:
        t = _load_transcript(name)
        if not t:
            continue
        who = {k: (v or {}).get("name") for k, v in (t.get("speakers") or {}).items()}
        for seg in (t.get("segments") or []):
            label = who.get(seg.get("speaker")) or seg.get("speaker") or "?"
            if who.get(seg.get("speaker")):
                names.add(label)
            lines.append(f"{label}: {seg['text']}")
            if sum(len(x) for x in lines) > max_chars:
                truncated = True
                break
        if truncated:
            break
    return {
        "start": _fmt_time(match.get("start")),
        "minutes": round((match.get("end", 0) - match.get("start", 0)) / 60, 1),
        "clips": len(match.get("clips", [])),
        "identified_speakers": sorted(names),
        "truncated": truncated,
        "text": "\n".join(lines),
    }


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
    for v in pipeline.labelling_queue(limit=limit):
        out.append({
            "id": v["person_id"],
            "minutes": round(v["seconds"] / 60, 1),
            "clips": v["clips"],
            "closest_named": [{"name": c["name"], "score": c["score"]}
                              for c in v["candidates"]],
            "said": v["text"][:400],
        })
    return out


@server.tool(description="What the local agent recorded from conversations: "
                         "tasks, events, notes, facts or topics.")
def recorded_items(kind: str = "notes", limit: int = 50) -> list:
    import agent_runner
    if kind not in agent_runner.KINDS:
        return [{"error": f"kind must be one of {list(agent_runner.KINDS)}"}]
    return agent_runner.load_items(kind, limit=limit)


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
        server.settings.host = a.host
        server.settings.port = a.port
        server.run(transport="streamable-http")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
