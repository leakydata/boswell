"""The server must keep answering while it thinks.

2026-09-13: naming a voice made the interface unreachable. `_rematch_clips`
walks every transcript in the archive re-matching voiceprints, every caller
of it was an `async def` endpoint, and so it ran on the event loop holding
the GIL. uvicorn stopped accepting connections for the duration -- thirteen
queued on a listening socket, 1700% CPU from numpy underneath, and systemd
reporting the service as active and running, which it was.

The shape of the bug is what these guard: not that re-matching is slow, but
that a slow thing ran where nothing else could run.
"""
import ast
import os

HERE = os.path.dirname(__file__)
SERVER = os.path.join(HERE, "..", "web", "server.py")


def _tree():
    with open(SERVER) as f:
        return ast.parse(f.read()), f


def test_no_async_function_calls_the_blocking_version():
    """The whole bug in one assertion."""
    tree = ast.parse(open(SERVER).read())
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("_rematch_clips",
                                         "_rematch_serialised")):
                bad.append(f"{fn.name}() at line {node.lineno}")
    assert not bad, (
        "these run a whole-archive re-match on the event loop and the server "
        "stops answering while they do: " + ", ".join(bad))


def test_the_wrapper_actually_leaves_the_loop():
    src = open(SERVER).read()
    body = src[src.index("async def _rematch_async"):]
    body = body[:body.index("\n\n\n")]
    assert "asyncio.to_thread" in body


def test_two_rematches_cannot_interleave():
    """Moving it off the loop is what makes concurrency possible at all, and
    it rewrites every transcript it touches."""
    src = open(SERVER).read()
    assert "_REMATCH_LOCK" in src
    body = src[src.index("def _rematch_serialised"):]
    body = body[:body.index("\n\n\n")] if "\n\n\n" in body else body[:400]
    assert "_REMATCH_LOCK" in body, "the serialised path must take the lock"


def test_every_endpoint_that_relabels_still_does():
    """The fix must not have quietly dropped the work."""
    src = open(SERVER).read()
    for fn in ("api_label", "api_voices_name", "api_ungroup",
               "api_voices_scan", "api_voices_recheck", "api_rematch"):
        i = src.index(f"def {fn}(")
        nxt = src.find("\n@app.", i)
        block = src[i:nxt if nxt > 0 else i + 4000]
        assert "_rematch_async(" in block, f"{fn} no longer re-matches"
