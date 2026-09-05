"""Reading back what the device wrote to its card.

Choosing FAT over raw sectors was a bet that something else could read these
files later. These cover the parsing rules that decide whether a real card
full of real audio survives that bet.
"""
import io
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))

import read_card as rc

FRAME_HDR = 12


def header(codec=20, rate=16000, boot=0xE9CD, magic=b"BSWL", ver=2):
    return (magic + bytes([ver, codec]) + struct.pack("<HI", rate, boot)
            + b"\0" * 4)


def frame(t_ms=0, flags=0x11, nsamples=320, body=b"\x01\x02\x03"):
    hdr = struct.pack("<HBBhHI", 0, flags, 0, 0, nsamples, t_ms)
    return hdr + body


def record(payload, ver=2, crc=None):
    out = struct.pack("<H", len(payload))
    if ver >= 2:
        out += bytes([rc.crc8(payload) if crc is None else crc])
    return out + payload


def test_a_good_header_parses():
    h = rc.read_header(io.BytesIO(header()))
    assert h["codec"] == 20 and h["codec_name"] == "Opus"
    assert h["rate"] == 16000 and h["boot_id"] == 0xE9CD


def test_bad_magic_is_refused():
    with pytest.raises(rc.BadFile):
        rc.read_header(io.BytesIO(header(magic=b"XXXX")))


def test_a_future_version_is_refused_rather_than_guessed_at():
    with pytest.raises(rc.BadFile):
        rc.read_header(io.BytesIO(header(ver=99)))


def test_the_crc_matches_the_one_the_firmware_writes():
    # Same polynomial and init as rec_crc.h, which is compiled and tested
    # natively -- this is the other half of that agreement.
    assert rc.crc8(b"") == 0xFF
    assert rc.crc8(b"\x00") == 0xF3


def test_a_truncated_header_is_refused():
    with pytest.raises(rc.BadFile):
        rc.read_header(io.BytesIO(b"BSWL"))


def test_frames_are_read_back_in_order():
    buf = io.BytesIO(b"".join(record(frame(t_ms=t)) for t in (0, 20, 40)))
    assert len(list(rc.read_frames(buf, 2))) == 3


def test_version_1_files_still_read():
    # Written before the checksum existed. Refusing them would strand real
    # recordings to gain a guarantee they were never written with.
    buf = io.BytesIO(b"".join(record(frame(t_ms=t), ver=1) for t in (0, 20)))
    got = list(rc.read_frames(buf, 1))
    assert len(got) == 2
    assert all(ok for _, ok in got)


def test_a_scrambled_payload_is_caught():
    # The length chain walks straight past this: the lengths are intact and
    # only the bytes between them are wrong.
    buf = io.BytesIO(record(frame(), crc=0x00) + record(frame(t_ms=20)))
    got = list(rc.read_frames(buf, 2))
    assert len(got) == 2, "one bad record must not end the file"
    assert got[0][1] is False
    assert got[1][1] is True


def test_a_corrupt_record_is_reported_not_decoded(tmp_path):
    p = tmp_path / "x.bwl"
    p.write_bytes(header() + record(frame(), crc=0x00) + record(frame(t_ms=20)))
    h = rc.read_file(str(p), want_audio=False)
    assert h["corrupt"] == 1
    assert h["frames"] == 2


def test_a_torn_tail_keeps_what_came_before_it():
    # The battery went during a batch. Everything before the tear is real
    # audio; refusing the file over its last few milliseconds would throw
    # away the recording to protect the recording.
    good = b"".join(record(frame(t_ms=t)) for t in (0, 20, 40))
    buf = io.BytesIO(good + struct.pack("<H", 900) + b"\x01\x02")
    assert len(list(rc.read_frames(buf, 2))) == 3


def test_a_zero_length_record_stops_rather_than_loops():
    buf = io.BytesIO(record(frame()) + struct.pack("<H", 0) + b"junk")
    assert len(list(rc.read_frames(buf, 2))) == 1


def test_an_absurd_length_is_treated_as_a_tear():
    buf = io.BytesIO(record(frame()) + struct.pack("<H", 60000))
    assert len(list(rc.read_frames(buf, 2))) == 1


def _write(tmp_path, payloads, **kw):
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(header(**kw) + b"".join(record(x) for x in payloads))
    return str(p)


def test_a_whole_file_reports_what_is_in_it(tmp_path):
    path = _write(tmp_path, [frame(t_ms=t) for t in (0, 20, 40)])
    h = rc.read_file(path, want_audio=False)
    assert h["frames"] == 3
    assert not h["torn"]
    assert h["first_ms"] == 0 and h["last_ms"] == 40


def test_a_torn_file_says_so(tmp_path):
    p = tmp_path / "x.bwl"
    p.write_bytes(header() + record(frame()) + struct.pack("<H", 500) + b"ab")
    h = rc.read_file(str(p), want_audio=False)
    assert h["torn"]
    assert h["frames"] == 1


def test_the_frame_decides_the_codec_not_the_file(tmp_path):
    # A backlog written to flash before a firmware change is replayed after
    # it, so the file header and the frame can disagree for exactly as long
    # as it takes to drain. The frame is the one that knows.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "host", "read_card.py")).read()
    assert "flags" in src and "payload[:FRAME_HEADER_LEN]" in src
    assert "hdr[\"codec\"]" not in src.split("def read_file")[1].split("def ")[0]


def test_a_frame_too_short_to_have_a_header_is_counted_not_crashed_on(tmp_path):
    path = _write(tmp_path, [b"\x00\x01", frame()])
    h = rc.read_file(path, want_audio=False)
    assert h["short"] == 1
    assert h["frames"] == 2
