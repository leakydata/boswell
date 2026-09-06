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
