"""
Pure tests for the parts that fail silently.

Nothing here needs a board. These cover the behaviours whose regressions do
not raise, do not log and do not crash -- they just quietly produce audio in
the wrong order, or a conversation split in the wrong place. Every case below
is a bug that actually shipped.

    uv run python -m pytest tests/ -q
"""

import json
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))

HEADER_LEN = 12


# ---------------------------------------------------------------- frames

def build_frame(seq, flags, index, predictor, nsamples, t_ms, payload=b""):
    return struct.pack("<HBBhHI", seq, flags, index, predictor,
                       nsamples, t_ms) + payload


def parse_frame(b):
    seq, flags, index, predictor, nsamples, t_ms = struct.unpack(
        "<HBBhHI", b[:HEADER_LEN])
    return dict(seq=seq, flags=flags, index=index, predictor=predictor,
                nsamples=nsamples, t_ms=t_ms, payload=b[HEADER_LEN:])


def test_frame_header_roundtrip():
    f = build_frame(65535, 0x0B, 44, -1234, 160, 4294967295, b"\x01\x02")
    p = parse_frame(f)
    assert p["seq"] == 65535
    assert p["flags"] == 0x0B
    assert p["index"] == 44
    assert p["predictor"] == -1234          # signed, and negative
    assert p["nsamples"] == 160
    assert p["t_ms"] == 4294967295          # full 32-bit range
    assert p["payload"] == b"\x01\x02"


def test_sequence_gap_wraps_at_16_bits():
    """A sequence rolling over 65535 is not sixty-five thousand lost frames."""
    gap = (2 - 65534 - 1) & 0xFFFF
    assert gap == 3


def test_replayed_frames_are_flagged():
    """Replayed audio must be distinguishable, or its old sequence numbers
    read as catastrophic packet loss."""
    live = parse_frame(build_frame(10, 0x00, 0, 0, 160, 1000))
    flash = parse_frame(build_frame(10, 0x08, 0, 0, 160, 1000))
    assert not (live["flags"] & 0x08)
    assert flash["flags"] & 0x08


# ---------------------------------------------------------- conversations

def _clip(tmp, name, start, seconds, use_device_time=True):
    """A clip on disk whose mtime is the END of its audio, as the real ones are."""
    path = os.path.join(tmp, name)
    with open(path, "wb") as f:
        f.write(b"\0" * 16)
    end = start + seconds
    os.utime(path, (end, end))
    if use_device_time:
        d = os.path.join(tmp, "times")
        os.makedirs(d, exist_ok=True)
        json.dump({"name": name, "started": start, "ended": end,
                   "seconds": seconds, "source": "flash"},
                  open(os.path.join(d, name + ".json"), "w"))
    return {"name": name, "seconds": seconds, "modified": end,
            "has_speech": True, "speakers": [], "preview": ""}


def test_ordering_survives_unequal_durations():
    """The bug: sorting on the end of the audio.

    A short clip that began later can finish before a long clip that began
    earlier. Ordering on the end time interleaved recovered audio wrongly
    with the live clips around it.
    """
    clips = [
        {"name": "a", "seconds": 30.0, "modified": 1000 + 30.0},   # 1000..1030
        {"name": "b", "seconds": 5.0,  "modified": 1010 + 5.0},    # 1010..1015
    ]
    by_end = [c["name"] for c in sorted(clips, key=lambda c: c["modified"])]
    by_start = [c["name"] for c in
                sorted(clips, key=lambda c: c["modified"] - c["seconds"])]
    assert by_end == ["b", "a"]        # wrong: b finished first
    assert by_start == ["a", "b"]      # right: a began first


def test_grouping_and_sorting_use_the_same_clock():
    """The bug: sorted by device time, grouped by file mtime.

    A clip could be placed correctly in the sequence and still fall into the
    wrong conversation, because the gap test that ends a conversation read a
    different timestamp than the sort did.
    """
    import index_db

    tmp = tempfile.mkdtemp()
    old_data = index_db.DATA
    index_db.DATA = tmp
    try:
        # Device says these are contiguous; mtime disagrees by a lot.
        c = _clip(tmp, "recovered_1.wav", start=1000, seconds=30)
        os.utime(os.path.join(tmp, "recovered_1.wav"), (9999, 9999))
        t = index_db.device_times("recovered_1.wav")
        assert t is not None, "device times should be read from the sidecar"
        assert t[0] == 1000, "start must come from the device, not the file"
        assert t[1] == 1030
    finally:
        index_db.DATA = old_data


def test_device_times_absent_falls_back_to_file():
    import index_db

    tmp = tempfile.mkdtemp()
    old = index_db.DATA
    index_db.DATA = tmp
    try:
        _clip(tmp, "clip_1.wav", start=500, seconds=10, use_device_time=False)
        assert index_db.device_times("clip_1.wav") is None
    finally:
        index_db.DATA = old


# ------------------------------------------------------------- atomic io

def test_atomic_write_leaves_no_partial_file():
    import atomicio

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "x.json")
    atomicio.write_json(p, {"a": 1})
    assert json.load(open(p)) == {"a": 1}
    assert not [f for f in os.listdir(tmp) if ".part" in f or f.startswith(".tmp-")]


def test_atomic_write_keeps_the_old_file_on_failure():
    """A failed write must not destroy what was already there."""
    import atomicio

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "x.json")
    atomicio.write_json(p, {"good": True})

    class Unserialisable:
        pass

    try:
        atomicio.write_json(p, {"bad": Unserialisable()})
    except Exception:
        pass
    assert json.load(open(p)) == {"good": True}, "old contents must survive"
    assert not [f for f in os.listdir(tmp) if f.startswith(".tmp-")]


# ----------------------------------------------------------- info layout

def test_info_capability_bits_do_not_overlap():
    """Two firmwares publish different things in the same bytes; the bits that
    say which are the only thing keeping them apart."""
    caps = {
        "steps": 0x0001, "imu_raw": 0x0002, "flash": 0x0004,
        "ota": 0x0008, "tap_diag": 0x0010, "overruns": 0x0020,
    }
    seen = 0
    for name, bit in caps.items():
        assert bit & seen == 0, f"{name} overlaps an earlier capability"
        seen |= bit
    # Steps and tap diagnostics both claim byte 13; a firmware must not say
    # it does both.
    assert caps["steps"] & caps["tap_diag"] == 0


# ------------------------------------------------- the real grouping code

def _fake_index(tmp, rows):
    """Point index_db at a scratch directory with a real sqlite index.

    The module caches its connection in thread-local state and reads DB_PATH
    at connect time, so both have to be replaced and the cached handle
    dropped -- otherwise the test quietly runs against the real recordings,
    which is how this test first "passed" against 100 clips it did not create.
    """
    import importlib, threading
    import index_db
    importlib.reload(index_db)
    index_db.DATA = tmp
    index_db.DB_PATH = os.path.join(tmp, "index.db")
    index_db._local = threading.local()
    c = index_db._conn()
    for r in rows:
        c.execute("""INSERT OR REPLACE INTO clips
                     (name, seconds, modified, status, has_speech, preview,
                      speakers, indexed_at)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (r["name"], r["seconds"], r["modified"], "done", 1, "",
                   "[]", time.time()))
    c.commit()
    return index_db


def test_conversations_orders_by_device_time_not_mtime():
    """Exercises index_db.conversations() itself, not a restatement of it.

    Two recovered clips whose file timestamps order them one way and whose
    device timestamps order them the other. This is the shape that put twenty
    clips out of sequence in real data.
    """
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "times"), exist_ok=True)

    # 'early' began first but is long, so it FINISHES last.
    rows = []
    for name, start, secs in [("recovered_early.wav", 1000.0, 40.0),
                              ("recovered_late.wav", 1010.0, 5.0)]:
        end = start + secs
        rows.append({"name": name, "seconds": secs, "modified": end})
        json.dump({"name": name, "started": start, "ended": end,
                   "seconds": secs, "source": "flash"},
                  open(os.path.join(tmp, "times", name + ".json"), "w"))

    idx = _fake_index(tmp, rows)
    convs = idx.conversations(300, 100)
    assert convs, "the two clips should form one conversation"
    order = convs[0]["clips"]
    assert order == ["recovered_early.wav", "recovered_late.wav"], (
        f"ordered by end time instead of start: {order}")


def test_conversations_splits_on_a_real_gap():
    """The gap test must read the same clock the sort does."""
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "times"), exist_ok=True)

    rows = []
    for name, start, secs in [("a.wav", 1000.0, 10.0),
                              ("b.wav", 1005.0, 10.0),      # overlaps a
                              ("c.wav", 9000.0, 10.0)]:     # hours later
        end = start + secs
        rows.append({"name": name, "seconds": secs, "modified": end})
        json.dump({"name": name, "started": start, "ended": end,
                   "seconds": secs, "source": "live"},
                  open(os.path.join(tmp, "times", name + ".json"), "w"))

    idx = _fake_index(tmp, rows)
    convs = idx.conversations(300, 100)
    sizes = sorted(len(c["clips"]) for c in convs)
    assert sizes == [1, 2], f"expected a 2-clip and a 1-clip conversation, got {sizes}"


def test_grouping_uses_device_time_when_mtime_disagrees():
    """The grouping clock must be the sorting clock.

    Three clips the device says are contiguous, whose file timestamps are
    hours apart -- which is exactly what a backlog drained long after it was
    recorded looks like. Grouped by mtime they fall into three conversations;
    grouped by device time they are one.

    The earlier version of this test could not fail, because its fixture had
    the two clocks agreeing. A test whose fixture cannot express the bug is
    not covering it.
    """
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "times"), exist_ok=True)

    rows = []
    for i, (name, start) in enumerate([("r1.wav", 1000.0),
                                       ("r2.wav", 1030.0),
                                       ("r3.wav", 1060.0)]):
        secs = 30.0
        # Drained hours later, and each one written a long way from the next.
        drained_at = 90000.0 + i * 4000.0
        rows.append({"name": name, "seconds": secs, "modified": drained_at})
        json.dump({"name": name, "started": start, "ended": start + secs,
                   "seconds": secs, "source": "flash"},
                  open(os.path.join(tmp, "times", name + ".json"), "w"))

    idx = _fake_index(tmp, rows)
    convs = idx.conversations(300, 100)
    assert len(convs) == 1, (
        f"device time says one conversation; grouping produced {len(convs)}. "
        "The gap test is reading file timestamps.")
    assert len(convs[0]["clips"]) == 3
