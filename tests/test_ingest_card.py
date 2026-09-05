"""Bringing a docked card's recordings into the archive.

Each of these is a way to corrupt an archive quietly rather than fail loudly:
importing the same audio twice, importing a file that was only half copied,
or stamping a clip with a time nobody actually knew.
"""
import json
import os
import struct
import sys

import numpy as np
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import ingest_card as ic
import read_card as rc


def header(codec=20, rate=16000, boot=0xE9CD, epoch=1788644288, ver=2):
    return (b"BSWL" + bytes([ver, codec])
            + struct.pack("<HII", rate, boot, epoch))


def frame(t_ms=0, flags=0x11, nsamples=320, body=b"\x01\x02\x03"):
    return struct.pack("<HBBhHI", 0, flags, 0, 0, nsamples, t_ms) + body


def record(payload):
    return struct.pack("<H", len(payload)) + bytes([rc.crc8(payload)]) + payload


def test_the_ledger_key_includes_the_size():
    # The device appends. A file ingested at 300 KB and grown to 600 KB
    # before the next dock is not the same file, and its second half has
    # never been read.
    import inspect
    src = inspect.getsource(ic.ledger_key)
    assert "st_size" in src


def test_an_unreadable_file_is_left_on_the_card(tmp_path, monkeypatch):
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(b"NOTBOSWELL" + b"\0" * 20)
    out = ic.ingest_file(str(p), [], {}, write=True)
    assert "UNREADABLE" in out
    assert "left on the card" in out
    assert p.exists(), "nothing may delete a file it could not read"


def test_a_file_that_fails_is_not_recorded_as_ingested(tmp_path):
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(b"NOTBOSWELL" + b"\0" * 20)
    ledger = {}
    ic.ingest_file(str(p), [], ledger, write=True)
    assert ledger == {}, "a failed file must be retried next dock"


def test_a_known_epoch_places_the_clip_in_real_time(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(ic, "TIMES", str(tmp_path / "data" / "times"))
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(header(epoch=1700000000)
                  + b"".join(record(frame(t_ms=t)) for t in (1000, 1020, 1040)))

    monkeypatch.setattr(rc, "decode_frame",
                        lambda *a, **k: np.zeros(320, dtype=np.int16))
    ic.ingest_file(str(p), [], {}, write=True)

    times = os.listdir(ic.TIMES)
    rec = json.load(open(os.path.join(ic.TIMES, times[0])))
    assert rec["time_known"] is True
    # epoch + device_ms, not "now"
    assert abs(rec["started"] - (1700000000 + 1.0)) < 0.01


def test_no_epoch_means_the_time_is_flagged_not_invented(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(ic, "TIMES", str(tmp_path / "data" / "times"))
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(header(epoch=0)
                  + b"".join(record(frame(t_ms=t)) for t in (1000, 1020)))
    monkeypatch.setattr(rc, "decode_frame",
                        lambda *a, **k: np.zeros(320, dtype=np.int16))
    ic.ingest_file(str(p), [], {}, write=True)

    rec = json.load(open(os.path.join(ic.TIMES, os.listdir(ic.TIMES)[0])))
    assert rec["time_known"] is False, \
        "a clip whose time was never known must say so, not look like a fact"


def test_audio_the_archive_already_holds_is_not_imported_again(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(ic, "TIMES", str(tmp_path / "data" / "times"))
    monkeypatch.setattr(rc, "decode_frame",
                        lambda *a, **k: np.zeros(320, dtype=np.int16))
    # Two seconds of frames, not a handful. is_duplicate widens the span it
    # is asked about by EDGE_SLACK_MS at each end, which is nothing against a
    # real clip and everything against a fifth of a second -- so a too-short
    # fixture fails the coverage threshold and tests the fixture, not the code.
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(header() + b"".join(record(frame(t_ms=t))
                                      for t in range(1000, 3000, 20)))

    held = []
    out1 = ic.ingest_file(str(p), held, {}, write=True)
    assert "1 new" in out1 or "new" in out1
    # Same file, same audio, empty ledger -- dedup alone has to catch it.
    out2 = ic.ingest_file(str(p), held, {}, write=False)
    assert "0 new" in out2


def test_a_file_with_no_boot_id_is_kept_rather_than_matched(tmp_path, monkeypatch):
    # boot_id 0 means the audio predates the run that wrote the file, so its
    # device milliseconds cannot be compared with anything. Erring toward
    # keeping is right: a duplicate can be merged later, a discard cannot.
    monkeypatch.setattr(ic, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(ic, "TIMES", str(tmp_path / "data" / "times"))
    monkeypatch.setattr(rc, "decode_frame",
                        lambda *a, **k: np.zeros(320, dtype=np.int16))
    p = tmp_path / "b00000000_0000.bwl"
    p.write_bytes(header(boot=0) + b"".join(record(frame(t_ms=t))
                                            for t in (1000, 1020)))
    held = []
    ic.ingest_file(str(p), held, {}, write=True)
    out = ic.ingest_file(str(p), held, {}, write=False)
    assert "0 already held" in out, "unmatchable audio must not be silently dropped"


def test_records_that_fail_their_checksum_are_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "DATA", str(tmp_path / "data"))
    monkeypatch.setattr(ic, "TIMES", str(tmp_path / "data" / "times"))
    monkeypatch.setattr(rc, "decode_frame",
                        lambda *a, **k: np.zeros(320, dtype=np.int16))
    good = frame(t_ms=1000)
    bad = frame(t_ms=1020)
    p = tmp_path / "b0000e9cd_0000.bwl"
    p.write_bytes(header() + record(good)
                  + struct.pack("<H", len(bad)) + b"\x00" + bad)
    out = ic.ingest_file(str(p), [], {}, write=False)
    assert "failed checksum" in out
