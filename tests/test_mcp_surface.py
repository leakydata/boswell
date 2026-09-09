"""Everything Boswell does, reachable by a model -- over MCP or over argv.

The MCP server was described in its own docstring as "read-only by design"
for months after it grew tools that write. This file is the check that the
surface and the description of it stay the same thing.
"""
import asyncio
import json
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "host"))
sys.path.insert(0, os.path.join(ROOT, "web"))

import boswell_mcp                                          # noqa: E402
import boswell_cli                                          # noqa: E402


def tools():
    return {t.name: t for t in asyncio.run(boswell_mcp.server.list_tools())}


def read(p):
    with open(os.path.join(ROOT, p)) as f:
        return f.read()


def test_every_part_of_boswell_has_a_tool():
    """The gaps this closes, each of which had a screen and no tool: the
    unit-level meaning index, the button presses, who a voice is, whether
    anything is recording, whether the backlog is transcribed, and the
    reviewing pass itself.
    """
    have = tools()
    for name in ("search_units", "list_marks", "list_topics", "list_sounds",
                 "name_voice", "set_voice_kind", "recorders",
                 "diagnose_recorder", "transcription_status",
                 "transcribe_missing", "rebuild_index", "review_conversation",
                 "agent_status", "delete_clips"):
        assert name in have, f"{name} is not reachable by a model"


def test_nothing_here_can_start_recording():
    """A disarmed recorder is nearly always a decision about the room
    somebody is in. Diagnosing why one is not recording is useful; reversing
    that decision unasked is not, so the connect, arm and unmute routes have
    no tool -- deliberately, and this is where that is written down.
    """
    src = read("host/boswell_mcp.py")
    for route in ("/api/recorders/pair", "/api/recorders/restart",
                  "/api/recorders/quiet", "/api/recorders/scan",
                  "/api/omi/settings"):
        assert route not in src, f"{route} is reachable from a model"
    assert "tool to connect, arm, or unmute" in src


def test_the_docstring_no_longer_claims_to_be_read_only():
    src = read("host/boswell_mcp.py")
    head = src[:src.index('"""', src.index('"""') + 3)]
    assert "Read-only by design" not in head
    assert "delete_recorded" in src and "record_fact" in src


def test_a_stale_meaning_index_is_named_rather_than_read_as_silence():
    """search_units over an index nobody rebuilt returns nothing, which is
    indistinguishable from an archive where nothing was said about it.
    Nothing rebuilds that index on a schedule, so this is the common case.
    """
    src = read("host/boswell_mcp.py")
    fn = src[src.index("def search_units("):]
    fn = fn[:fn.index("\n@server.tool")]
    assert "rebuild_index" in fn


def test_the_live_half_asks_the_server_rather_than_reimplementing_it():
    """The transcription queue is an object inside the running server and the
    Bluetooth link is a connection it owns. A second process loading Whisper
    to clear a backlog would fight the first for the card and the files.
    """
    src = read("host/boswell_mcp.py")
    assert "def _api(" in src
    fn = src[src.index("def _api("):]
    fn = fn[:fn.index("\n# ----")]
    # And a server that is not running says so, rather than raising.
    assert "not answering" in fn
    assert "BOSWELL_TOKEN" in fn


def test_an_unreachable_server_is_an_answer_not_a_traceback():
    old = os.environ.get("BOSWELL_URL")
    os.environ["BOSWELL_URL"] = "http://127.0.0.1:9"      # discard port
    try:
        r = boswell_mcp._api("/api/queue")
    finally:
        if old is None:
            os.environ.pop("BOSWELL_URL", None)
        else:
            os.environ["BOSWELL_URL"] = old
    assert r["ok"] is False
    assert "uv run web/server.py" in r["hint"], "it does not say what to start"


# ---- the CLI --------------------------------------------------------------


def test_the_cli_is_the_registry_rather_than_a_second_list():
    """A hand-written command list drifts from the tools the day one is
    added. This one reflects, so it cannot.
    """
    src = read("host/boswell_cli.py")
    assert "list_tools" in src
    assert "call_tool" in src
    listing = boswell_cli._describe(tools())
    for name in tools():
        assert name in listing, f"{name} is missing from the CLI listing"


def test_the_cli_turns_argv_strings_into_what_the_schema_asks_for():
    assert boswell_cli._coerce("5", {"type": "integer"}) == 5
    assert boswell_cli._coerce("0.5", {"type": "number"}) == 0.5
    assert boswell_cli._coerce("yes", {"type": "boolean"}) is True
    # A list arrives either way somebody would type it.
    assert boswell_cli._coerce(["a.wav", "b.wav"], {"type": "array"}) == \
        ["a.wav", "b.wav"]
    assert boswell_cli._coerce("a.wav,b.wav", {"type": "array"}) == \
        ["a.wav", "b.wav"]
    # An optional argument is typed "string or null" and must not become "null".
    assert boswell_cli._coerce("x", {"type": ["string", "null"]}) == "x"


def test_a_tool_that_reports_failure_exits_non_zero():
    """These tools answer with {"ok": false} rather than raising, which is
    right for a model reading the answer and invisible to a shell that only
    sees an exit code -- so `boswell x && boswell y` would run y regardless.
    """
    src = read("host/boswell_cli.py")
    fn = src[src.index("def main("):]
    assert 'r.get("ok") is False' in fn and 'r.get("error")' in fn
    assert "return 1" in fn


def test_every_tool_has_a_parser_built_from_its_schema():
    for name, t in tools().items():
        ap, props = boswell_cli._parser_for(name, t)
        for arg in (t.input_schema or {}).get("properties", {}):
            assert f"--{arg}" in ap.format_usage() or arg in ap.format_help(), \
                f"{name} cannot be given {arg} from a shell"


def test_an_unknown_tool_suggests_rather_than_stack_traces():
    assert boswell_cli.main(["search_unit"]) == 2       # near-miss of search_units
    assert boswell_cli.main([]) == 0                    # bare invocation lists


def test_provenance_is_counted_rather_than_recited():
    """Every write carries the clips it came from, and a widened conversation
    is hundreds of them -- so ten tasks came back as two thousand file names
    around the sentences that mattered. Unreadable on a terminal, and a large
    part of a context window for a list nobody reads.
    """
    item = {"text": "x", "_clips": [f"c{i}.wav" for i in range(80)]}
    got = boswell_mcp._trim_clips(item)
    assert len(got["_clips"]) == boswell_mcp.SHOW_CLIPS
    assert got["_clips_total"] == 80
    assert got["_conversation"] == "c0.wav", "no way back to the conversation"
    # A short list is left exactly as it is.
    small = {"text": "x", "_clips": ["a.wav"]}
    assert boswell_mcp._trim_clips(small) == small


def test_a_review_hands_back_exactly_what_it_read():
    """The clips a review widened to are the argument to mark_reviewed, and
    they are not "the conversation": a 300-second gap groups a whole evening
    of a never-stopping recorder into one, and marking that on the strength
    of one window's reading retired 412 clips after 79 were read.
    """
    src = read("host/boswell_mcp.py")
    fn = src[src.index("def review_conversation("):]
    fn = fn[:fn.index("\n@server.tool")]
    assert "clips_read" in fn
    assert 'r.pop("clips")' not in fn, "the caller cannot mark what was read"

    # And mark_reviewed does not resolve a span on the caller's behalf.
    fn = src[src.index("def mark_reviewed("):]
    fn = fn[:fn.index("\ndef ")]
    assert "index_db.conversations" not in fn


def test_the_queue_reaches_the_whole_archive_not_only_today():
    """400 clips is the last two or three hours on a recorder that never
    stops, so a queue meant for working through the archive only ever showed
    today -- and a clip from this afternoon was already outside it.
    """
    src = read("host/boswell_mcp.py")
    assert "index_db.conversations(300, 400)" not in src
    assert boswell_mcp.SCAN_CLIPS >= 20000

    agent = read("web/agent_runner.py")
    assert "conversations(int(CONTEXT_GAP), 400)" not in agent, \
        "a review of anything older than today gets no surrounding context"


def test_a_partly_read_conversation_says_so():
    # Reviewing is per window and grouping is per gap; the two are not the
    # same size, so a long conversation is worked through in pieces.
    src = read("host/boswell_mcp.py")
    fn = src[src.index("def unreviewed_conversations("):]
    fn = fn[:fn.index("\ndef ")]
    assert "already_reviewed" in fn
    assert "REVIEWED_SHARE" in fn


# ---- what the read path can reach -----------------------------------------


def test_a_conversation_is_addressed_by_time_not_by_a_clip_budget():
    """`limit` on index_db.conversations is a clip budget, and 400 clips is
    about three hours on a recorder that never stops -- one 350-clip evening
    consumes almost the whole window. Measured: get_conversation on a clip
    from this afternoon answered `no conversation contains omi_1788904400.wav`
    while get_clip on the same name returned its transcript in full. The data
    was never missing; the grouping could not reach it.
    """
    src = read("host/boswell_mcp.py")
    assert "gap_seconds=300, limit=400" not in src
    fn = src[src.index("def _conversation_of("):]
    fn = fn[:fn.index("\ndef ")]
    assert "since=" in fn and "until=" in fn, "still grouping by budget"

    import index_db
    import inspect
    assert "since" in inspect.signature(index_db.conversations).parameters
    assert "since" in inspect.signature(index_db.list_clips).parameters


def test_every_listed_conversation_can_be_opened():
    """The property the round-trip needs, and the cheapest test that would
    have caught it: list_conversations names first_clip and its description
    points at get_conversation, but a conversation whose first clip fell
    outside the window listed and would not open.
    """
    for cv in boswell_mcp.list_conversations(limit=8):
        first = cv.get("first_clip")
        if not first:
            continue
        got = boswell_mcp.get_conversation(first, max_chars=200)
        assert "error" not in got, \
            f"{first} was listed but {got.get('error')!r}"


def test_one_conversation_gap_shared_by_everything_that_means_one():
    """It was four numbers: index_db said 60, server.py grouped at 300 in two
    places, both MCP read tools passed 300, and agent_runner carried its own
    CONTEXT_GAP of 300 under a comment claiming it was "the same gap the
    recordings view groups conversations by". Measured on this archive, 60
    gives 533 conversations with a p90 of twelve minutes; 300 gives 173 and a
    single 371-clip, 183-minute block covering six unrelated subjects.
    """
    import agent_runner
    import index_db
    assert agent_runner.CONTEXT_GAP == float(index_db.CONVERSATION_GAP)
    for f in ("web/server.py", "host/boswell_mcp.py", "web/agent_runner.py"):
        src = read(f)
        assert "conversations(300" not in src and "gap_seconds=300" not in src, \
            f"{f} still carries its own conversation gap"


def test_identified_is_a_fraction_because_a_boolean_lied():
    """`identified` went true on the first named line. Measured on tonight's
    18:45 block it read as solved over 2,191 segments of which 200 carried a
    name -- 9.1% -- and the server instructions tell a model to check that
    field before attributing a quote. It was also computed from the first 40
    clips of a 367-clip conversation.
    """
    src = read("host/boswell_mcp.py")
    assert '"identified": identified' not in src
    assert 'clips", [])[:40]' not in src, "coverage from 11% of the evidence"
    cov = boswell_mcp._speaker_coverage([])
    for field in ("named_segments", "unnamed_segments", "identified_fraction"):
        assert field in cov


def test_a_long_conversation_comes_back_in_sections():
    """threads.sections() exists to prevent exactly the wall get_conversation
    was returning -- 2,026 lines over 193 minutes covering dogs, a trade show,
    a commentary channel, robotics tutorials and two AI videos. It found 28
    clean breaks in that same material and they were discarded.
    """
    src = read("host/boswell_mcp.py")
    fn = src[src.index("def get_conversation("):]
    fn = fn[:fn.index("\n@server.tool")]
    assert "import threads" in fn and "for_conversation" in fn
    assert "sections_total" in fn


# ---- what may be offered, and what may be recorded ------------------------


def test_no_name_is_offered_below_the_measured_noise_floor():
    """speaker_store's own figures: same person p10 0.715, different people
    p99 0.572, MATCH_LOW 0.55. The queue was offering its top three whatever
    they scored -- measured at 0.35, 0.37 and 0.47, with voice 57's three
    closest being Nathan Jones, Dave Rubin and Sam Witteveen, which ranks
    nothing. After the floor, 0 of 8 voices in the queue offer a name.
    """
    src = read("web/pipeline.py")
    fn = src[src.index("def labelling_queue("):]
    fn = fn[:fn.index("\ndef ")]
    assert "sdb.MATCH_LOW" in fn, "candidates are not floored"
    assert "KIND_MEDIA" in fn, "a voice off a screen can be offered as the wearer"

    import pipeline
    import speaker_store
    for v in pipeline.labelling_queue(limit=10):
        for c in v["candidates"]:
            assert c["score"] >= speaker_store.MATCH_LOW, \
                f"{c['name']} offered at {c['score']}"


def test_an_item_names_who_said_it_not_how_loud_the_room_was():
    """The first gate here counted the mix of the clips: refuse when less than
    a quarter of the speech in them came from a named person. It was built on
    a real measurement -- the one bad item at 13.1% named-person, every good
    item at 99.7% -- and it was the wrong shape, which the measurement could
    not show because the archive held no example of the case that matters.

    The case that matters: the wearer talks at the screen. His words and a
    video's are interleaved in the same clips and the video does most of the
    talking. Measured on the conversation containing "I just want a way to
    have AI take notes from videos that I watch" -- 4.0% named person. The mix
    rule refused the owner's own dictated request because a robotics tutorial
    was louder.
    """
    import tools_impl
    assert not hasattr(tools_impl, "NAMED_SHARE_MIN"), \
        "still judging a clip by its mix"
    assert "topics" not in tools_impl.GATED_KINDS
    for k in ("notes", "tasks", "events", "facts"):
        assert k in tools_impl.GATED_KINDS

    src = read("host/tools_impl.py")
    for fn in ("def add_note", "def add_task", "def add_calendar_event",
               "def remember_fact"):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 10)]
        assert "said_by" in body, f"{fn} does not record who said it"
        assert "_attributable(" in body, f"{fn} is not gated"

    # And the model is required to supply it, not merely allowed to.
    for t in tools_impl.SCHEMAS:
        f = t["function"]
        if f["name"] in ("add_note", "add_task", "add_calendar_event",
                         "remember_fact"):
            assert "said_by" in f["parameters"]["required"]


def test_the_gate_admits_a_sentence_spoken_over_a_video():
    import tools_impl
    real = tools_impl._context_clips
    try:
        tools_impl.set_context([])          # no clips: only the name is judged
        assert tools_impl._attributable("tasks", "Nathan Jones") is None
        for bad, why in (("", "required"),
                         ("SPEAKER_02", "diarizer label")):
            r = tools_impl._attributable("tasks", bad)
            assert r and why in r["error"], f"{bad!r} was not refused"
    finally:
        tools_impl.set_context(real)


def test_unreadable_transcripts_are_not_a_reason_to_refuse():
    import tools_impl
    real = tools_impl._context_clips
    try:
        tools_impl.set_context(["nonexistent_probe.wav"])
        assert tools_impl._attributable("notes", "Nathan Jones") is None
    finally:
        tools_impl.set_context(real)
