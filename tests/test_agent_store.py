"""The agent store is appended by one process and rewritten by another.

Records are appended as the agent saves them, while edit, delete, clear and
the id backfill read the whole file and rename a new one into place. A rename
replaces the file, so an append landing between a rewriter's read and its
rename goes away with the old inode -- the agent reports the item saved and it
is not there.

Measured before the lock existed: 191 of 300 records lost. This test runs the
real thing in real processes, because a single-process approximation of that
race proves nothing about it.
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap

import pytest

HOST = os.path.join(os.path.dirname(__file__), "..", "host")


def _sandbox(tmp_path):
    """A copy of tools_impl with its store rooted under tmp_path."""
    (tmp_path / "host").mkdir()
    (tmp_path / "data" / "agent").mkdir(parents=True)
    shutil.copy(os.path.join(HOST, "tools_impl.py"), tmp_path / "host" / "tools_impl.py")
    return str(tmp_path / "host")


APPEND = """
    import sys
    sys.path.insert(0, __HOST__)
    import tools_impl
    for i in range(%d):
        tools_impl._append("notes", {"title": "n%%d" %% i})
"""

REWRITE = """
    import sys, json, os, time
    sys.path.insert(0, __HOST__)
    import tools_impl
    p = os.path.join(tools_impl.STORE, "notes.jsonl")
    for _ in range(%d):
        %s
            rows = []
            if os.path.exists(p):
                for line in open(p):
                    line = line.strip()
                    if line:
                        try: rows.append(json.loads(line))
                        except Exception: pass
            time.sleep(0.004)
            tmp = p + ".part"
            with open(tmp, "w") as f:
                f.write("".join(json.dumps(d) + chr(10) for d in rows))
            os.replace(tmp, p)
        time.sleep(0.002)
"""

N_APPEND, N_REWRITE = 150, 30


def _race(tmp_path, guard):
    host = _sandbox(tmp_path)
    (tmp_path / "a.py").write_text(
        textwrap.dedent(APPEND % N_APPEND).replace("__HOST__", repr(host)))
    (tmp_path / "r.py").write_text(
        textwrap.dedent(REWRITE % (N_REWRITE, guard)).replace("__HOST__", repr(host)))
    a = subprocess.Popen([sys.executable, str(tmp_path / "a.py")])
    r = subprocess.Popen([sys.executable, str(tmp_path / "r.py")])
    a.wait(timeout=120)
    r.wait(timeout=120)
    store = tmp_path / "data" / "agent" / "notes.jsonl"
    return sum(1 for line in store.read_text().splitlines() if line.strip())


@pytest.mark.slow
def test_lock_keeps_every_append(tmp_path):
    kept = _race(tmp_path, "with tools_impl.store_lock():")
    assert kept == N_APPEND, f"lost {N_APPEND - kept} of {N_APPEND} appends"


@pytest.mark.slow
def test_without_the_lock_records_are_lost(tmp_path):
    """The test can detect the fault it claims to prevent."""
    kept = _race(tmp_path, "if True:")
    assert kept < N_APPEND, ("no records were lost without the lock, so this "
                             "test proves nothing about the one with it")
