"""An Omi as a second recorder.

Its Opus is byte-identical to Boswell's -- 20 ms frames, 16 kHz, 32 kbps,
CELT restricted low delay -- so nothing about the audio needs translating.
What differs is everything around it: a three-byte header instead of twelve,
no timestamp, and no boot id. Each of those absences needs a decision rather
than a default, and these are the decisions.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omi_capture as oc


def test_the_codec_ids_map_to_frame_lengths():
    # Their numbering is by frame length, not by codec: 20 is Opus at 10 ms
    # on the devkit, 21 is Opus at 20 ms on the CV 1.
    assert oc.CODEC_FRAMES[20] == 160
    assert oc.CODEC_FRAMES[21] == 320


def test_the_header_is_three_bytes():
    # [packet:u16][index:u8]. Boswell's is twelve and carries a timestamp;
    # this one does not, which is the whole reason Run exists.
    assert oc.HEADER_LEN == 3


def test_a_device_is_named_the_way_the_archive_names_them():
    assert oc.norm_id("C4:B3:FD:7F:1E:91") == "c4b3fd7f1e91"
    assert oc.norm_id("c4b3fd7f1e91") == "c4b3fd7f1e91"
    assert oc.norm_id(None) is None


def test_a_counter_going_backwards_starts_a_new_run():
    # The counter restarting is the only evidence available that the device
    # rebooted, and without a new id the two sessions look like one.
    r = oc.Run()
    first = r.id
    assert not r.note(100)
    assert not r.note(101)
    assert r.note(3)
    assert r.id != first


def test_small_reordering_is_not_a_reboot():
    r = oc.Run()
    first = r.id
    r.note(100)
    assert not r.note(97)      # within the slack
    assert r.id == first


def test_device_ms_comes_from_the_packet_counter():
    # Packets are 20 ms apart by construction, so the counter is a monotonic
    # device clock in everything but name -- which is what de-duplication
    # needs and an Omi does not otherwise provide.
    c = oc.Clipper("c4b3fd7f1e91")
    c.add(100, np.zeros(320, dtype=np.int16))
    c.add(101, np.zeros(320, dtype=np.int16))
    assert c.first_ms == 100 * oc.FRAME_MS
    assert c.last_ms == 101 * oc.FRAME_MS


def test_a_reboot_files_what_is_held_before_mixing(tmp_path, monkeypatch):
    # Audio from the run that ended does not belong in a clip with audio
    # from the one that started.
    import clipwriter
    monkeypatch.setattr(clipwriter, "DATA", str(tmp_path))
    monkeypatch.setattr(clipwriter, "TIMES", str(tmp_path / "times"))
    c = oc.Clipper("c4b3fd7f1e91")
    c.add(100, np.zeros(320, dtype=np.int16))
    before = c.run.id
    c.add(1, np.zeros(320, dtype=np.int16))
    assert c.clips == 1, "the held audio should have been filed"
    assert c.run.id != before


def test_the_time_is_marked_unknown(tmp_path, monkeypatch):
    # An Omi has no clock and nothing tells it one, so arrival time is a
    # placement of last resort. Saying so is what stops anything downstream
    # reading it as the device's own account of when this happened.
    import json, clipwriter
    monkeypatch.setattr(clipwriter, "DATA", str(tmp_path))
    monkeypatch.setattr(clipwriter, "TIMES", str(tmp_path / "times"))
    c = oc.Clipper("c4b3fd7f1e91")
    c.add(100, np.zeros(320, dtype=np.int16))
    path = c.flush()
    rec = json.load(open(os.path.join(str(tmp_path / "times"),
                                      os.path.basename(path) + ".json")))
    assert rec["time_known"] is False
    assert rec["device_id"] == "c4b3fd7f1e91"
    assert rec["source"] == "omi"


def test_clips_from_two_recorders_are_not_confused():
    # The point of the whole exercise: one archive, two devices, and a
    # sixteen-bit boot id that will collide between them eventually.
    import dedup
    omi = {"boot_id": 500, "device_ms": [10000, 40000],
           "device_id": "c4b3fd7f1e91"}
    assert not dedup.is_duplicate(500, (10000, 40000), [omi],
                                  device_id="d966cfbb58a4")
    assert dedup.is_duplicate(500, (10000, 40000), [omi],
                              device_id="c4b3fd7f1e91")


# ---------------------------------------------------- two recorders, two panels
def read_file(p):
    with open(os.path.join(HERE, "..", p)) as f:
        return f.read()


def test_the_device_panel_names_the_device_it_is_connected_to():
    # "XIAO-MIC" was hardcoded, so the panel said it whatever was on the
    # other end. Fine while there was one recorder; a label that lies the
    # moment there are two.
    html = read_file("web/static/index.html")
    assert '$("ver").textContent = s.connected ? "XIAO-MIC"' not in html
    assert "s.device_name || s.device_address" in html


def test_the_server_publishes_what_the_device_calls_itself():
    assert 'self.state["device_name"]' in read_file("web/server.py")


def test_the_omi_panel_says_which_omi():
    html = read_file("web/static/index.html")
    assert 'id="omiaddr"' in html
    assert '$("omiaddr").textContent = s.address' in html


def test_the_omi_status_is_read_from_a_file_not_the_radio():
    # The radio is exclusive. A status query that took the connection would
    # interrupt the recording it was reporting on.
    src = read_file("web/server.py")
    fn = src[src.index("async def api_omi("):]
    fn = fn[:fn.index("\n@app.")]
    assert "omi_status.json" in fn
    assert "BleakClient" not in fn


def test_a_daemon_that_stopped_writing_is_not_running():
    # Whatever its last line claimed.
    src = read_file("web/server.py")
    fn = src[src.index("async def api_omi("):]
    fn = fn[:fn.index("\n@app.")]
    assert "stale" in fn


def test_the_daemon_syncs_before_it_streams():
    # Streaming first leaves hours of stored audio behind every time somebody
    # walks back into range.
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session("):]
    fn = fn[:fn.index("\nasync def run(")]
    assert fn.index("omi_sync.sync") < fn.index("omi_capture.capture")


def test_a_failed_sync_does_not_skip_the_live_stream():
    # The backlog will still be there next time; the conversation happening
    # now will not.
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session("):]
    fn = fn[:fn.index("\nasync def run(")]
    sync_block = fn[fn.index("omi_sync.sync"):fn.index("omi_capture.capture")]
    assert "except Exception" in sync_block


def test_the_daemon_keeps_saying_it_is_recording():
    # publish() was called once when recording started and never again, so
    # after two minutes of perfectly healthy recording the status went stale
    # and the interface said "not running". A thing that works must not look
    # identical to a thing that stopped.
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session("):]
    fn = fn[:fn.index("\nasync def run(")]
    assert "async def heartbeat()" in fn
    assert "asyncio.create_task(heartbeat())" in fn


def test_the_heartbeat_stops_when_the_session_does():
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session("):]
    fn = fn[:fn.index("\nasync def run(")]
    assert "finally:" in fn and "stop.set()" in fn


def test_capture_reports_progress_to_whoever_started_it():
    src = read_file("host/omi_capture.py")
    assert "on_progress=None" in src
    assert 'on_progress({"clips"' in src


def test_the_interface_says_what_it_has_heard():
    # "recording" alone for an hour is indistinguishable from "stuck saying
    # recording".
    html = read_file("web/static/index.html")
    assert "clip` \n" not in html
    assert "this session" in html
