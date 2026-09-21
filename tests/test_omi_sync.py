"""Pulling what an Omi recorded while it was away.

The property that shapes this file: **reading consumes**. The device advances
its own read pointer as it confirms bytes sent, so a packet handed over is a
packet gone whatever the host does with it, and asking again returns
"sequence out of range". There is no second chance, and that is why the bytes
are spooled before anything tries to understand them.
"""
import os
import struct
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omi_sync as osy


def packet(ts, frames):
    body = b"".join(bytes([len(f)]) + f for f in frames)
    body = body[:osy.PACKET_BYTES - osy.STAMP_BYTES]
    body += b"\0" * (osy.PACKET_BYTES - osy.STAMP_BYTES - len(body))
    return struct.pack(">I", ts) + body


def test_a_packet_is_a_timestamp_then_length_prefixed_frames():
    ts, frames = osy.parse_packet(packet(1788530084, [b"abc", b"defgh"]))
    assert ts == 1788530084
    assert frames == [b"abc", b"defgh"]


def test_the_timestamp_is_big_endian():
    # sys_put_be32 on their side. Read little-endian it is 2764937834, which
    # is a plausible-looking number and a wrong one.
    ts, _ = osy.parse_packet(packet(1788530084, [b"x"]))
    assert ts == 1788530084


def test_padding_after_the_last_frame_is_not_a_frame():
    # A partly filled packet is zero-padded, and a zero length would
    # otherwise be read as an endless run of empty frames.
    ts, frames = osy.parse_packet(packet(1, [b"ab"]))
    assert frames == [b"ab"]


def test_a_frame_running_past_the_end_is_refused():
    # A real overrun: a plausible length claimed with ten bytes left. Reading
    # it would take whatever follows in the buffer -- the next packet's
    # timestamp -- and hand it to the decoder as audio.
    body = bytes([4]) + b"abcd"                    # one honest frame
    body += b"\0" * (osy.PACKET_BYTES - osy.STAMP_BYTES - len(body) - 10)
    body += bytes([200]) + b"\0" * 9              # claims 200, has 9
    p = struct.pack(">I", 1) + body
    assert len(p) == osy.PACKET_BYTES
    ts, frames = osy.parse_packet(p)
    assert frames == [b"abcd"], "the overrunning length must stop the walk"


def test_a_short_packet_is_refused():
    ts, frames = osy.parse_packet(b"\0" * 10)
    assert ts is None and frames == []


def test_the_bytes_are_spooled_before_they_are_decoded():
    # The device has already let go by the time they arrive. Losing them to a
    # decode error or a crash would lose them for good.
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    fn = src[src.index("async def sync("):src.index("def drain_spool(")]
    assert "spool.write(raw)" in fn
    assert "os.fsync(spool.fileno())" in fn
    # and nothing decodes inside the transfer loop
    assert "decode_opus" not in fn
    assert "parse_packet" not in fn


def test_the_spool_is_only_removed_once_its_audio_is_filed():
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    fn = src[src.index("def drain_spool("):]
    fn = fn[:fn.index("\n\nasync def ") if "\n\nasync def " in fn else len(fn)]
    assert fn.index("sink.flush()") < fn.index("os.remove(path)")


def test_there_is_no_keep_option_that_does_not_keep():
    # --keep read as though it left the audio on the device. It did not: the
    # pointer moved from 640812 to 646762 across three runs that all claimed
    # to keep. An option that lies about a one-way operation is worse than
    # no option.
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    assert '"--keep"' not in src


def test_offloaded_audio_knows_when_it_happened():
    # Unlike the live stream. The device stamps what it stores and says
    # nothing about what it streams, and the difference has to survive into
    # the archive or the clips are placed at the time they were collected.
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    fn = src[src.index("    def flush(self):"):src.index("\nclass Link")]
    assert "time_known=True" in fn
    assert 'source="omi-card"' in fn


def test_a_re_read_is_not_a_second_copy():
    src = open(os.path.join(HERE, "..", "host", "omi_sync.py")).read()
    fn = src[src.index("    def flush(self):"):src.index("\nclass Link")]
    assert "dedup.is_duplicate" in fn


def test_audio_stored_before_the_clock_was_set_is_filed_by_arrival(tmp_path,
                                                                   monkeypatch):
    """After a reset, and before the first sync sets the clock, the device
    stamps stored packets with zero. Those were skipped without a word, so a
    sync reported "197 packet(s) -> 0 clip(s)" -- a device with nothing to
    say, rather than twenty seconds of speech going in the bin.

    There is no honest way to say when it happened from the device's side.
    Arrival is the placement live-streamed audio already gets, and it is
    marked the same way, so nothing claims a precision it does not have.
    """
    import json, os, struct
    import numpy as np
    import omi_sync, clipwriter

    monkeypatch.setattr(clipwriter, "DATA", str(tmp_path))
    monkeypatch.setattr(clipwriter, "TIMES", str(tmp_path / "times"))
    monkeypatch.setattr(omi_sync, "SPOOL", str(tmp_path / "spool"))
    os.makedirs(omi_sync.SPOOL)
    # The plumbing is under test, not the codec.
    monkeypatch.setattr(omi_sync, "decode_opus",
                        lambda b, n, r: np.zeros(n, dtype=np.int16))

    blob = b""
    for _ in range(2000):                      # about 160 seconds
        p = struct.pack(">I", 0)               # the zero stamp
        for _ in range(4):
            p += bytes([8]) + b"\x01" * 8
        blob += p[:omi_sync.PACKET_BYTES].ljust(omi_sync.PACKET_BYTES, b"\0")
    (tmp_path / "spool" / "c4b3fd7f1e91_1.raw").write_bytes(blob)

    sink = omi_sync.drain_spool("c4b3fd7f1e91", quiet=True)
    assert sink.undated == 2000
    assert sink.clips > 0, "undated audio is still being thrown away"

    wavs = sorted(p for p in os.listdir(str(tmp_path)) if p.endswith(".wav"))
    assert len(wavs) == sink.clips
    # Cut like every other clip: one offload can hold hours, and a single
    # wav of it would be neither playable nor transcribable.
    assert len(wavs) > 1, "the whole offload was written as one clip"
    # Clips are named for the second they end, so chunks sharing an arrival
    # time would share a filename and overwrite one another.
    assert len(set(wavs)) == len(wavs)

    recs = [json.load(open(os.path.join(str(tmp_path / "times"), w + ".json")))
            for w in wavs]
    assert all(r["time_known"] is False for r in recs), \
        "it would claim the device said when this happened"
    # No counter came with the packets, and dedup drops a span holding None
    # rather than guessing -- keeping audio beats discarding it on a guess.
    assert all(r["device_ms"] == [None, None] for r in recs)
    # In order, and contiguous: the run reads as the stretch it was.
    for a, b in zip(recs, recs[1:]):
        assert a["ended"] <= b["started"] + 0.01
    assert abs(sum(r["seconds"] for r in recs) - 160.0) < 0.5
    assert os.listdir(omi_sync.SPOOL) == [], "the spool was not cleared"


# --- reading consumes, so an error must not also be a deletion -----------
#
# The property at the top of this file, applied to the failure path. Bytes
# the device has handed over are gone from it whatever the host does, so
# dropping them while unwinding an exception is not a failed transfer, it is
# a deletion -- and one that reports itself as "the sync achieved nothing".

class _FakeClient:
    """A device that sends some packets and then stops answering."""

    def __init__(self, link, packets, then):
        self.link, self.packets, self.then = link, packets, then

    async def write_gatt_char(self, _uuid, _cmd, response=True):
        for p in self.packets:
            self.link.on_notify(None, bytes([osy.N_DATA]) + p)
        if self.then == "disconnect":
            raise RuntimeError("device disconnected")


def test_a_batch_that_stops_early_keeps_what_already_arrived():
    import asyncio
    link = osy.Link.__new__(osy.Link)
    link.info = None
    link.data = bytearray()
    link.done = link.ack = None
    link._begun = asyncio.Event()
    pkts = [packet(1788530000 + i, [b"aa"]) for i in range(3)]
    link.c = _FakeClient(link, pkts, then="silence")

    async def go():
        return await link.read(0, 400, timeout=0.15)

    try:
        asyncio.run(go())
        assert False, "a read that never completes must not look like success"
    except osy.TransferInterrupted as e:
        assert len(e.salvaged) == 3 * osy.PACKET_BYTES, (
            "three whole packets arrived and must be handed back, not "
            "discarded -- the device has already let go of them")


def test_a_dropped_link_keeps_what_already_arrived():
    import asyncio
    link = osy.Link.__new__(osy.Link)
    link.info = None
    link.data = bytearray()
    link.done = link.ack = None
    link._begun = asyncio.Event()
    link.c = _FakeClient(link, [packet(1788530000, [b"aa"])],
                         then="disconnect")
    try:
        asyncio.run(link.read(0, 400, timeout=0.15))
        assert False, "expected TransferInterrupted"
    except osy.TransferInterrupted as e:
        assert len(e.salvaged) == osy.PACKET_BYTES


def test_a_trailing_fragment_is_not_salvaged():
    """Half a packet cannot be parsed and its timestamp is in the half that
    did not arrive."""
    link = osy.Link.__new__(osy.Link)
    link.data = bytearray(packet(1788530000, [b"aa"]) + b"\x01\x02\x03")
    assert len(link._whole_packets()) == osy.PACKET_BYTES


def test_a_refusal_is_still_an_ordinary_error():
    """The device answers a refusal before sending anything, so there is
    nothing to salvage and TransferInterrupted would be misleading."""
    import inspect
    src = inspect.getsource(osy.Link.read)
    refusal = src[src.index("device refused the read"):]
    assert "TransferInterrupted" not in refusal.split("if not ok")[0]


# --- and a file that did not convert is not deleted ----------------------

def test_a_spool_that_will_not_decode_is_kept_not_deleted(tmp_path,
                                                          monkeypatch):
    """`Sink.add()` counts every decode failure and nothing read the count,
    so a decoder regression could turn a consumed transfer into zero clips
    and then remove the only copy."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(osy, "SPOOL", str(spool))
    monkeypatch.setattr(osy, "KEPT", str(spool / "kept"))
    monkeypatch.setattr(osy, "held_records", lambda: [])

    raw = spool / "dev_1.raw"
    # Opus frames that are not Opus, so every one fails to decode.
    raw.write_bytes(packet(1788530000, [b"\xff\xff\xff\xff"]) * 2)

    osy.drain_spool("dev", quiet=True)

    assert not raw.exists(), "the file should have been moved out of the way"
    assert (spool / "kept" / "dev_1.raw").exists(), (
        "a transfer that would not decode must be kept -- the device has "
        "already let go of it")


def test_a_clean_spool_is_still_deleted(tmp_path, monkeypatch):
    """Keeping everything would fill the disk with duplicates of the archive."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(osy, "SPOOL", str(spool))
    monkeypatch.setattr(osy, "KEPT", str(spool / "kept"))
    monkeypatch.setattr(osy, "held_records", lambda: [])
    raw = spool / "dev_2.raw"
    raw.write_bytes(b"\0" * osy.PACKET_BYTES)      # parses, no frames, no bad
    osy.drain_spool("dev", quiet=True)
    assert not raw.exists()
    assert not (spool / "kept" / "dev_2.raw").exists()
