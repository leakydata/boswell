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


class TestMergeItems:
    """Recall lets the agent see duplicates; merge lets it fix them.

    Six copies of one sentence about asphalt were in the real store when this
    was written, all from a single clip that says it once, because every
    review before recall existed started from nothing.
    """

    def _store(self, tmp_path, monkeypatch, rows):
        import importlib
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))
        import tools_impl
        importlib.reload(tools_impl)
        store = tmp_path / "agent"
        store.mkdir()
        monkeypatch.setattr(tools_impl, "STORE", str(store))
        (store / "tasks.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows))
        return tools_impl, store

    def _rows(self):
        return [
            {"_id": "a", "text": "attempt 350 with asphalt", "_clips": ["c1.wav"]},
            {"_id": "b", "text": "Attempt 350 with asphalt.", "_clips": ["c2.wav"]},
            {"_id": "c", "text": "attempt to reach 350, asphalt", "_clips": ["c1.wav"]},
            {"_id": "z", "text": "something else entirely", "_clips": ["c9.wav"]},
        ]

    def test_duplicates_are_folded_into_one(self, tmp_path, monkeypatch):
        t, store = self._store(tmp_path, monkeypatch, self._rows())
        r = t.merge_items("tasks", "a", ["b", "c"], text="Reach 350 with pure asphalt.")
        assert r["ok"] and r["merged"] == 2
        rows = [json.loads(l) for l in (store / "tasks.jsonl").read_text().splitlines() if l]
        assert len(rows) == 2
        kept = [x for x in rows if x["_id"] == "a"][0]
        assert kept["text"] == "Reach 350 with pure asphalt."
        assert kept["_merged"] == 2

    def test_provenance_is_carried_across(self, tmp_path, monkeypatch):
        """The clips the dropped entries came from must not be lost -- they are
        the only way back to what was actually said."""
        t, store = self._store(tmp_path, monkeypatch, self._rows())
        t.merge_items("tasks", "a", ["b", "c"])
        rows = [json.loads(l) for l in (store / "tasks.jsonl").read_text().splitlines() if l]
        kept = [x for x in rows if x["_id"] == "a"][0]
        assert set(kept["_clips"]) == {"c1.wav", "c2.wav"}

    def test_unrelated_entries_untouched(self, tmp_path, monkeypatch):
        t, store = self._store(tmp_path, monkeypatch, self._rows())
        t.merge_items("tasks", "a", ["b", "c"])
        rows = [json.loads(l) for l in (store / "tasks.jsonl").read_text().splitlines() if l]
        assert any(x["_id"] == "z" for x in rows)

    def test_unknown_keep_id_reports_failure(self, tmp_path, monkeypatch):
        t, _ = self._store(tmp_path, monkeypatch, self._rows())
        r = t.merge_items("tasks", "nope", ["b"])
        assert not r["ok"]

    def test_bad_kind_rejected(self, tmp_path, monkeypatch):
        t, _ = self._store(tmp_path, monkeypatch, self._rows())
        assert not t.merge_items("../evil", "a", ["b"])["ok"]

    def test_empty_drop_list_is_a_noop(self, tmp_path, monkeypatch):
        t, store = self._store(tmp_path, monkeypatch, self._rows())
        before = (store / "tasks.jsonl").read_text()
        assert not t.merge_items("tasks", "a", [])["ok"]
        assert (store / "tasks.jsonl").read_text() == before
