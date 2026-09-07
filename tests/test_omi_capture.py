"""An Omi as a second recorder.

Its Opus is byte-identical to Boswell's -- 20 ms frames, 16 kHz, 32 kbps,
CELT restricted low delay -- so nothing about the audio needs translating.
What differs is everything around it: a three-byte header instead of twelve,
no timestamp, and no boot id. Each of those absences needs a decision rather
than a default, and these are the decisions.
"""
import re
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
    # What the device calls itself comes first, whatever else follows it.
    assert "s.device_name ||" in html


def test_the_panel_still_names_its_recorder_while_it_is_switched_off():
    """A recorder that is off is still the recorder the panel is about.

    device_name is only known once something answers, so a page loaded while
    the recorder is off labelled the panel "Device" and its settings "this
    recorder" -- and that vagueness lands exactly when the other recorder is
    the one still running and telling them apart matters most.
    """
    html = read_file("web/static/index.html")
    assert "s.device_wanted" in html, "no fallback to the name we look for"
    assert 'el.textContent = boswellName' in html, "the badges do not use it"
    src = read_file("web/server.py")
    assert '"device_wanted": DEVICE_NAME' in src, "the server never sends it"


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
    # recording", so the panel carries the count and the duration behind it.
    # These moved out of the status line and into the stats row when the two
    # recorders were given the same panel; the requirement is that the figures
    # are on screen, not which line they sit on.
    html = read_file("web/static/index.html")
    assert "clip` \n" not in html
    assert "This session" in html          # the stat's label
    assert '$("omiclips").textContent' in html   # and something fills it
    assert "min of audio" in html or "` \u00b7 ${mins} min`" in html


# ------------------------------------------------------ what it says about itself
def test_the_stats_are_read_on_a_connection_somebody_already_has():
    # The radio is exclusive: whatever holds the device is the only thing
    # that can ask it anything.
    src = read_file("host/omi_capture.py")
    assert "async def read_stats(client)" in src


def test_every_stat_is_optional():
    # A firmware that does not offer one is not an error, and a stats read
    # must never be the reason a recording stops.
    src = read_file("host/omi_capture.py")
    fn = src[src.index("async def read_stats("):src.index("async def set_clock(")]
    assert "except Exception:" in fn and "pass" in fn


def test_the_characteristics_are_named_from_their_firmware():
    # Guessed from the numbers these are a list of hex strings.
    src = read_file("host/omi_capture.py")
    for name in ("OMI_MIC_GAIN", "OMI_CHARGING", "OMI_FEATURES",
                 "OMI_TIME_READ", "OMI_TIME_WRITE", "OMI_DIM_RATIO"):
        assert name in src


def test_the_clock_is_read_little_endian():
    # memcpy(&epoch_s, buf, sizeof(epoch_s)) on little-endian ARM, so the
    # wire format is little-endian -- not the big-endian their stored packets
    # and storage protocol use.
    src = read_file("host/omi_capture.py")
    fn = src[src.index("async def read_stats("):src.index("async def set_clock(")]
    assert '"little"' in fn


def test_a_drifted_clock_is_corrected():
    # It stamps the packets it stores, and those stamps are the only thing
    # that places offloaded audio. A device whose clock is wrong files real
    # conversations under the wrong hour and nothing downstream can tell.
    src = read_file("host/omid.py")
    assert "set_clock" in src
    assert "drift > 120" in src


def test_stopping_reaches_the_capture_loop():
    # The flag was only checked between sessions, which is never while
    # recording, so systemctl restart waited out its timeout and the process
    # was SIGKILLed -- taking the clip in hand with it.
    src = read_file("host/omi_capture.py")
    fn = src[src.index("async def capture("):]
    assert "should_stop" in fn
    assert "if should_stop and should_stop():" in fn


def test_the_unit_allows_time_to_finish_the_clip():
    assert "TimeoutStopSec" in read_file("host/omid.service")


# ------------------------------------------------ what changes while it runs
def test_the_battery_is_re_read_while_it_records():
    """A reading taken once a session is not a reading.

    read_stats() runs when the link opens and the same dict is republished
    for as long as the link holds, so the panel showed 100% for hours while
    the cell drained. Anything that changes has to be asked again.
    """
    import asyncio

    class FakeClient:
        """Answers the three volatile characteristics and nothing else."""
        def __init__(self, pct):
            self.pct, self.reads = pct, []

        async def read_gatt_char(self, uuid):
            self.reads.append(uuid)
            if uuid == oc.BATTERY:
                return bytes([self.pct])
            if uuid == oc.OMI_CHARGING:
                return bytes([0])
            if uuid == oc.OMI_TIME_READ:
                return (1788700000).to_bytes(4, "little")
            raise RuntimeError("not a volatile characteristic")

    c = FakeClient(61)
    out = asyncio.run(oc.read_volatile(c))
    assert out["battery"] == 61
    assert out["charging"] is False
    assert out["device_epoch"] == 1788700000
    # Stamped, so a reader comparing the clock measures drift and not the age
    # of the reading.
    assert "read_at" in out

    # Deliberately short: every read competes with the audio notifications on
    # the same radio, so the static fields are not asked again.
    assert oc.MODEL not in c.reads and oc.FIRMWARE not in c.reads


def test_a_failed_stats_read_is_not_an_error():
    # It must never be the reason a recording stops.
    import asyncio

    class Broken:
        async def read_gatt_char(self, uuid):
            raise RuntimeError("device went away")

    assert asyncio.run(oc.read_volatile(Broken())) == {}


def test_the_recorder_offers_the_fresh_readings_to_its_caller():
    # The radio is exclusive: the streaming loop is the only thing holding
    # the device, so it is the only thing that can ask it anything.
    import inspect
    sig = inspect.signature(oc.capture)
    assert "on_stats" in sig.parameters
    assert sig.parameters["stats_every"].default > 0


def test_the_daemon_keeps_its_published_battery_current():
    # Otherwise the fresh reading is taken and then thrown away.
    src = read_file("host/omid.py")
    assert "on_stats=stats.update" in src


# ------------------------------------------------- changing what can change
def test_a_setting_is_read_back_rather_than_assumed():
    """A firmware that clamps or ignores a value must not leave the interface
    showing a number the device never held."""
    import asyncio

    class Clamping:
        """Accepts a gain but keeps only the low three bits, as a device that
        disagrees with the host would."""
        def __init__(self):
            self.held = {}

        async def write_gatt_char(self, uuid, data, response=True):
            self.held[uuid] = bytes([data[0] & 0x07])

        async def read_gatt_char(self, uuid):
            return self.held[uuid]

    c = Clamping()
    got = asyncio.run(oc.apply_settings(c, mic_gain=30))
    assert got == {"mic_gain": 30 & 0x07}, "reported the asked-for value"


def test_a_refused_setting_does_not_stop_the_recording():
    import asyncio

    class Refusing:
        async def write_gatt_char(self, uuid, data, response=True):
            raise RuntimeError("not writable")

    assert asyncio.run(oc.apply_settings(Refusing(), mic_gain=6)) == {}


def test_only_the_named_settings_are_written():
    # Passing nothing must touch nothing: the loop is entered once a second.
    import asyncio

    class Loud:
        async def write_gatt_char(self, *a, **k):
            raise AssertionError("wrote with nothing to write")

    assert asyncio.run(oc.apply_settings(Loud())) == {}


def test_the_daemon_applies_a_request_by_id():
    """Asking for the value the device already holds is still an instruction.

    Compared by value, "no change needed" and "never arrived" look identical
    to whoever is watching the slider.
    """
    src = read_file("host/omid.py")
    assert 'want["id"] == applied["id"]' in src
    assert 'stats["applied_id"]' in src


def test_the_page_cannot_write_to_the_omi_itself():
    # The radio is exclusive and the daemon is holding it, so a setting is a
    # request left in a file rather than a write.
    src = read_file("web/server.py")
    assert "omi_wanted.json" in src


def test_a_setting_is_carried_out_once():
    """The request file is consumed, not left standing.

    Left in place it is reasserted on every reconnection, so a change made
    once quietly outlives the moment it was made -- and nothing in the
    interface shows that, because the interface shows only what the device
    currently holds.
    """
    import importlib, json as _json, tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import omid

    tmp = tempfile.mkdtemp()
    omid.WANTED = os.path.join(tmp, "omi_wanted.json")
    with open(omid.WANTED, "w") as f:
        _json.dump({"id": 5.0, "mic_gain": 6}, f)

    assert omid.read_wanted()["id"] == 5.0
    omid.clear_wanted(5.0)
    assert omid.read_wanted() is None, "the request survived being applied"


def test_a_newer_request_is_not_swallowed_by_an_older_one_completing():
    # A change written between reading the file and finishing the write to
    # the device must not be dropped by the older one tidying up after
    # itself.
    import json as _json, tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import omid

    tmp = tempfile.mkdtemp()
    omid.WANTED = os.path.join(tmp, "omi_wanted.json")
    with open(omid.WANTED, "w") as f:
        _json.dump({"id": 9.0, "mic_gain": 8}, f)

    omid.clear_wanted(5.0)          # the older request finishing
    assert omid.read_wanted()["id"] == 9.0, "the newer request was dropped"


# ----------------------------------------- why the link ended, kept on screen
def test_a_session_reports_restarts_of_the_recorder():
    """A counter reset means the device restarted mid-session. It is the one
    fact that tells "you walked out of range" from "it is dying", and it was
    only ever printed to a log."""
    src = read_file("host/omi_capture.py")
    assert "clipper.reboots = reboots[0]" in src
    assert "clipper.dropped = dropped[0]" in src


def test_the_last_reading_survives_the_device_going_away():
    # stats was rebuilt empty every session, so the last thing the device
    # said about itself was discarded at the moment it became useful.
    src = read_file("host/omid.py")
    assert 'LAST = {"stats": {}, "session": None}' in src
    assert 'stats = dict(LAST["stats"])' in src, "a new session starts blank"
    assert 'LAST["stats"] = dict(stats)' in src, "nothing is carried out"


def test_every_status_write_carries_what_was_last_known():
    # Otherwise the interface has to ask separately, and the states that
    # matter -- looking, lost, waiting -- are the ones that would not carry it.
    src = read_file("host/omid.py")
    body = src[src.index("def publish("):src.index("def publish(") + 400]
    assert 'fields.setdefault("stats"' in body
    assert 'fields["last_session"]' in body


def test_the_panel_says_how_old_a_battery_reading_is():
    # "96%" from an hour ago and "96%" from a minute ago are different facts.
    html = read_file("web/static/index.html")
    assert "battAge" in html
    assert "last link ran" in html


def test_what_was_known_survives_a_service_restart():
    """A restart is exactly when somebody is trying to work out what
    happened, and it was the moment the history was thrown away."""
    src = read_file("host/omid.py")
    assert "def _restore_last" in src
    assert "_restore_last()" in src.split("def _restore_last")[1], \
        "defined but never called"
    # The old state is not carried over: it described the previous run.
    body = src[src.index("def _restore_last"):]
    body = body[:body.index("\ndef ", 10)]
    assert '"state"' not in body


def test_restore_reads_a_real_status_file():
    """Exercised rather than asserted about: the point is that a battery
    reading and the last session come back after a restart."""
    import json as _json, tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import omid

    tmp = tempfile.mkdtemp()
    omid.STATUS = os.path.join(tmp, "omi_status.json")
    with open(omid.STATUS, "w") as f:
        _json.dump({"state": "recording", "at": 1788700000,
                    "stats": {"battery": 96, "read_at": 1788699000},
                    "last_session": {"seconds": 1800, "clips": 41,
                                     "reboots": 1}}, f)

    omid.LAST = {"stats": {}, "session": None}
    omid._restore_last()
    assert omid.LAST["stats"]["battery"] == 96
    assert omid.LAST["session"]["reboots"] == 1
    # The previous run's state is not adopted as this run's.
    assert "state" not in omid.LAST


def test_restore_survives_a_missing_or_broken_status_file():
    import tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import omid
    tmp = tempfile.mkdtemp()
    omid.STATUS = os.path.join(tmp, "gone.json")
    omid.LAST = {"stats": {}, "session": None}
    omid._restore_last()                      # must not raise
    with open(omid.STATUS, "w") as f:
        f.write("{ not json")
    omid._restore_last()
    assert omid.LAST == {"stats": {}, "session": None}


def test_a_connection_that_never_happened_is_not_a_session():
    """The retry loop attempts a connection every twenty-five seconds and
    each failure raises through the same block. Recording those as sessions
    overwrote the useful record -- half a minute, no clips, no frames --
    with a description of a link that never existed, and it survived about
    twenty-five seconds before being replaced by the next failure."""
    src = read_file("host/omid.py")
    body = src[src.index("        LAST[\"stats\"] = dict(stats)"):]
    body = body[:body.index("    return clipper")]
    assert 'if clipper is not None or beat.get("frames")' in body, \
        "every failed attempt is still recorded as a session"
    # And a session that streamed then died by exception must still be kept:
    # that is the one worth having.
    assert 'beat.get("frames")' in body


def test_a_session_says_whether_it_ended_cleanly():
    # A service restart and a device walking away look the same otherwise.
    src = read_file("host/omid.py")
    assert '"clean": clipper is not None' in src


def test_a_bogus_session_from_an_older_build_is_not_restored():
    """An older build recorded every failed connection attempt as a session.
    Restoring one carries that bug's output across the upgrade that fixed
    it, and it sits on screen describing a link that was never made."""
    import json as _json, tempfile
    sys.path.insert(0, os.path.join(HERE, "..", "host"))
    import omid
    tmp = tempfile.mkdtemp()
    omid.STATUS = os.path.join(tmp, "s.json")

    with open(omid.STATUS, "w") as f:
        _json.dump({"last_session": {"seconds": 25.0, "clips": 0,
                                     "frames": 0}}, f)
    omid.LAST = {"stats": {}, "session": None}
    omid._restore_last()
    assert omid.LAST["session"] is None, "the bug's output came back"

    with open(omid.STATUS, "w") as f:
        _json.dump({"last_session": {"seconds": 1800, "clips": 41,
                                     "frames": 59848}}, f)
    omid.LAST = {"stats": {}, "session": None}
    omid._restore_last()
    assert omid.LAST["session"]["clips"] == 41, "a real session was dropped"


def test_recording_is_claimed_only_once_audio_arrives():
    """The heartbeat said "recording" the moment it started -- before capture
    had connected, let alone received anything. A session that never found
    the device announced itself as recording every twenty seconds against a
    retry cycle of twenty-five, so the interface spent most of its time
    claiming to record from a device that was not there."""
    src = read_file("host/omid.py")
    beat = src[src.index("async def heartbeat"):]
    beat = beat[:beat.index("await asyncio.wait_for")]
    assert 'state=("recording" if beat["frames"] else "connecting")' in beat, \
        "recording is still claimed before any audio"


def test_connecting_does_not_count_as_connected():
    # Otherwise the panel, the badge and the diagnosis all inherit the lie.
    src = read_file("web/server.py")
    away = src[src.index("_AWAY = "):src.index("_AWAY = ") + 320]
    assert '"connecting"' in away


def test_the_wait_between_attempts_is_spent_listening():
    """The loop slept a fixed backoff between attempts, so it was deaf for
    about sixty seconds of every hundred and forty-five. A recorder that
    advertises only briefly -- after a button press, or between power-saving
    sleeps -- can pass entirely inside that gap."""
    src = read_file("host/omid.py")
    assert "async def wait_until_advertising" in src
    tail = src[src.index("wait = BACKOFF["):]
    tail = tail[:tail.index("publish(state=\"stopped\")")]
    assert "wait_until_advertising(addr, wait)" in tail, \
        "the loop still sleeps through the wait"


def test_a_scanner_that_will_not_start_does_not_become_a_crash_loop(monkeypatch):
    # A retry loop is the wrong place to discover that scanning is broken.
    #
    # Asserted by running it rather than by reading it. This used to grep the
    # source for `await asyncio.sleep(timeout)`, which stopped being the way
    # the wait is spent the moment it had to be interruptible -- and a test
    # that fails when the code is refactored but not when the behaviour
    # breaks is protecting the spelling, not the promise. The promise is: a
    # scanner that will not start is absorbed, and the wait is still a wait.
    import asyncio
    import time

    import omid

    class WillNotStart:
        def __init__(self, *a, **k):
            pass

        async def start(self):
            raise RuntimeError("no bluetooth")

        async def stop(self):
            pass

    async def no_such_device(_addr):
        return None

    fake = type(sys)("bleak")
    fake.BleakScanner = WillNotStart
    monkeypatch.setitem(sys.modules, "bleak", fake)
    monkeypatch.setattr(omid.omi_capture, "bluez_device", no_such_device)
    monkeypatch.setattr(omid, "searching", lambda: True)

    began = time.monotonic()
    got = asyncio.run(omid.wait_until_advertising("AA:BB:CC:DD:EE:FF", 1.0))
    took = time.monotonic() - began

    assert got is None
    # It waited rather than returning at once -- returning immediately is
    # what turns the caller's retry loop into a spin.
    assert took >= 0.9, f"gave up after {took:.2f}s instead of waiting"


def test_storage_is_never_read_while_audio_is_streaming():
    """The ring was read from the per-second tick during capture, and
    `ring["at"]` started at zero, so the "every sixty seconds" test was true
    immediately: every session opened a second notification subscription and
    sent a storage command one second into the audio stream.

    Recording stopped that afternoon and stayed broken -- sessions connected,
    took no frames and dropped. The storage protocol is for offload; using it
    underneath a live stream is not something the firmware offered.
    """
    src = read_file("host/omid.py")
    tick = src[src.index("    async def on_tick(client):"):]
    tick = tick[:tick.index("\n    beat = ")]
    assert "start_notify" not in tick, "still subscribing during capture"
    assert "ring_info" not in tick, "still asking storage during capture"
    # It is read where the storage characteristic is already being used:
    # on the sync's own connection, handed back as it asks.
    assert "on_ring=lambda info: note_ring(stats, info)" in src
    snc = read_file("host/omi_sync.py")
    ring = snc[snc.index("info = await link.ring_info()"):]
    ring = ring[:ring.index("waiting = info[")]
    assert "on_ring(info)" in ring, "the sync does not report what it read"


def test_syncing_is_not_announced_before_the_device_is_reached():
    """The daemon published "syncing" before attempting the sync, so a device
    that was not there produced a fresh status file saying "syncing" on every
    retry. The file never went stale, "syncing" is not one of the states the
    interface treats as away, and the recorder was reported as connected and
    transferring for hours while every attempt failed with "not found".

    "syncing" may only be published from the progress callback, which runs
    inside the connected client once packets are actually moving.
    """
    src = read_file("host/omid.py")
    body = src[src.index("async def one_session"):]
    body = body[:body.index("spool, took")]
    assert 'publish(state="syncing"' not in body, \
        "syncing is still announced before the sync is attempted"

    # And where it does appear, it is inside the progress callback.
    prog = src[src.index("progress=lambda"):]
    prog = prog[:prog.index("))")]
    assert 'state="syncing"' in prog, "nothing reports a sync in progress"


def test_a_sync_that_is_only_being_attempted_reads_as_away():
    # The status the daemon publishes while trying must be one the interface
    # scores as not-connected, or the badge inherits the lie.
    omid_src = read_file("host/omid.py")
    server_src = read_file("web/server.py")
    attempt = omid_src[omid_src.index("async def one_session"):]
    attempt = attempt[:attempt.index("spool, took")]
    state = re.search(r'publish\(state="([a-z ]+)", address=address, stats=stats\)',
                      attempt)
    assert state, "the pre-sync publish is gone or changed shape"
    away = server_src[server_src.index("_AWAY = "):]
    away = away[:away.index("\n\n\ndef ")]
    assert f'"{state.group(1)}"' in away, \
        f'the daemon publishes "{state.group(1)}" while trying, '\
        "but the interface counts that as connected"


def test_a_ring_read_never_stops_a_recording():
    # A figure on a panel is not a reason to lose a session.
    snc = read_file("host/omi_sync.py")
    call = snc[snc.index("on_ring(info)") - 120:]
    call = call[:200]
    assert "except Exception" in call


def test_the_storage_figure_is_not_asked_for_on_its_own_connection():
    """It was, and that connection had to find the device a second time --
    which for a recorder advertising in short windows mostly failed. The
    failure was swallowed as "a panel figure, never a reason to stop", so
    the storage line sat twenty-one hours stale beside a battery reading a
    minute old, with nothing to tell them apart. Read as current, it said
    the device held nine seconds of audio while it was recording normally;
    the very next sync pulled 304 packets.
    """
    src = read_file("host/omid.py")
    assert "async def read_ring_now" not in src, \
        "the ring is still asked for on a connection of its own"
    assert "def note_ring(stats, info)" in src
    # And it is filled from what the sync was already told.
    fn = src[src.index("def note_ring(stats, info)"):]
    fn = fn[:fn.index("\n\nasync def ")]
    assert 'info["write_seq"] - info["read_seq"]' in fn
    assert '"at": time.time()' in fn


def test_nothing_is_published_before_the_link_exists():
    """The beat started with the attempt, so a session that never reached the
    device still announced a state -- first "recording", then "connecting"
    once that was fixed. Both described an intention rather than a
    connection, and both were read as proof the device was there."""
    src = read_file("host/omid.py")
    beat = src[src.index("    async def heartbeat():"):]
    beat = beat[:beat.index("while not stop.is_set():")]
    assert "await linked.wait()" in beat, "the beat still runs before linking"
    assert "on_connected=linked.set" in src, "nothing ever sets it"
    # And the wait must be released on the way out, or a failed session
    # leaves the beat parked forever.
    assert "linked.set()" in src[src.index("stop.set()"):][:200]


def test_connected_means_notifications_are_running():
    # Called after start_notify, not after connect: a client that connects
    # and cannot subscribe has no audio path and is not a working link.
    src = read_file("host/omi_capture.py")
    i = src.index("await client.start_notify(OMI_AUDIO, on_frame)")
    assert "on_connected" in src[i:i + 400]


def test_a_recorder_that_was_just_seen_is_not_looked_for_again():
    """The wait returned True and dropped the device it had seen, so the
    session that followed made bleak discover it all over again -- three
    times, once each for stats, sync and capture.

    This recorder advertises in windows too short to find twice. An HCI
    capture of 200 seconds carried 1,036 advertisements from 45 devices and
    not one from this one, so the sighting is the scarce thing, and throwing
    it away is what turned "saw it advertise -- connecting now" straight
    into "device not found".
    """
    src = read_file("host/omid.py")
    fn = src[src.index("async def wait_until_advertising"):]
    fn = fn[:fn.index("\nasync def run(")]
    assert "return found[\"dev\"]" in fn, "the wait still discards what it saw"
    assert "return True" not in fn, "still reporting a sighting as a bare flag"

    loop = src[src.index("async def run(address=None"):]
    assert "sighted = await wait_until_advertising(addr, wait)" in loop
    assert "one_session(addr, quiet=quiet, dev=sighted)" in loop, \
        "the sighting never reaches the session"


def test_a_sighting_is_not_reused_after_the_session_it_belongs_to():
    # A device object is a handle on something that was there a moment ago.
    # Carrying a stale one into later retries is worse than looking again.
    src = read_file("host/omid.py")
    loop = src[src.index("async def run(address=None"):]
    body = loop[:loop.index("wait = BACKOFF[")]
    assert "sighted = None" in body, "a stale sighting outlives its session"


def test_every_connection_in_a_session_can_use_the_sighting():
    """All three -- stats, sync, capture -- discovered the device
    separately, so a device seen once still had to be found three more
    times. The readings no longer have a connection at all, so two are
    left, and both take what the wait saw."""
    assert "BleakClient(dev or address" in read_file("host/omi_sync.py")
    assert "BleakClient(dev or address" in read_file("host/omi_capture.py")
    # And omid itself opens none: it borrows the sync's.
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session"):src.index("\ndef note_ring")]
    assert "BleakClient(" not in fn, "a session still opens a third connection"


def test_a_device_bluez_is_holding_can_still_be_reached():
    """BlueZ auto-connects a device it has bonded and trusted. A connected
    peripheral stops advertising, and bleak finds devices by scanning for
    advertisements, so every connect then fails with "device not found" --
    while the recorder sits connected to this very machine.

    Measured here: bluetoothctl said Connected/Paired/Trusted yes, and the
    daemon reported "not found" for hours. Attaching to the object BlueZ
    already had read battery 100% and twelve services on the first try.
    Nothing about that state recovers on its own, so it has to be asked for.
    """
    src = read_file("host/omi_capture.py")
    assert "async def bluez_device" in src, "nothing asks BlueZ what it holds"
    fn = src[src.index("async def bluez_device"):]
    fn = fn[:fn.index("\nasync def capture(")]
    assert "org.bluez.Device1" in fn and "GetManagedObjects" in fn.replace(
        "call_get_managed_objects", "GetManagedObjects")
    # A known-but-disconnected device has an object too, and handing that to
    # BleakClient trades an honest "not found" for a confusing failure.
    assert 'props.get("Connected")' in fn
    # Linux only, and quiet about it -- this must not break a mac.
    assert "except ImportError:" in fn and "return None" in fn


def test_waiting_for_an_advertisement_that_cannot_come_is_not_a_wait():
    # A connected peripheral does not advertise. Waiting on one while BlueZ
    # holds the device burns the whole backoff to learn nothing.
    src = read_file("host/omid.py")
    fn = src[src.index("async def wait_until_advertising"):]
    fn = fn[:fn.index("\nasync def run(")]
    head = fn[:fn.index("from bleak import BleakScanner")]
    assert "bluez_device" in head, "it still waits before asking BlueZ"


def test_the_first_attempt_asks_too():
    # The deadlock is reachable before any wait has happened.
    src = read_file("host/omid.py")
    loop = src[src.index("async def run(address=None"):]
    pre = loop[:loop.index("await one_session(")]
    assert "bluez_device" in pre


def test_a_panel_reading_does_not_get_a_connection_of_its_own():
    """Stats were read first, on their own connection, before the sync and
    the stream -- and it failed on every session ("stats: TimeoutError")
    while the sync connected successfully seconds later. A recorder that
    advertises in short windows cannot be found three times over, and the
    first search spent the window.

    Battery is refreshed again during capture; the part that only happens
    here is the clock, and it can be set on a connection that already
    exists.
    """
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session"):]
    before = fn[:fn.index('publish(state="connecting"')]
    assert "BleakClient" not in before, \
        "the readings still open a connection of their own"
    assert "async def read_on(c)" in before, "nothing reads them at all"
    assert "on_client=read_on" in fn, "the readings never reach the sync"
    # The clock is the part that has nowhere else to happen.
    assert "set_clock" in before


def test_a_handle_that_failed_is_not_handed_on():
    """A sighting names a D-Bus object BlueZ made when it saw the device,
    and BlueZ drops that object once the device goes. Handing it to the
    stream after the sync has already failed on it turns an honest "not
    found" into "device 'dev_C4_B3_FD_7F_1E_91' not found" for a connection
    that has attempted nothing."""
    src = read_file("host/omid.py")
    fn = src[src.index("async def one_session"):]
    tail = fn[fn.index('print(f"sync:'):]
    tail = tail[:tail.index("# A heartbeat")]
    assert "dev = None" in tail, "a dead handle is still passed to the stream"


def test_a_signal_handler_does_not_print():
    """A signal can land in the middle of a print, and writing to the same
    buffered stream from inside the handler raises "reentrant call inside
    BufferedWriter". That killed the daemon outright, mid-session, exit code
    1 -- the line announcing a clean stop was what made it unclean.
    """
    src = read_file("host/omid.py")
    fn = src[src.index("    def stop(*_):"):]
    fn = fn[:fn.index("signal.signal(")]
    assert "print(" not in fn, "the signal handler still writes to stdout"
    # The loop still says it, where there is no handler to re-enter.
    assert 'print("stopping; finishing the clip in hand"' in src


def test_stored_audio_with_no_timestamp_is_kept_not_dropped():
    """The device stores audio before it has a clock to stamp it with --
    after a reset, or before a run's first sync -- and those packets carry a
    zero stamp. They were skipped without a word, so a sync reported "197
    packet(s) -> 0 clip(s)" and read as a device with nothing to say rather
    than twenty seconds of speech going in the bin.

    There is no honest way to say when it happened from the device's side,
    but arrival is exactly the placement live-streamed audio already gets,
    and it is marked the same way.
    """
    snc = read_file("host/omi_sync.py")
    loop = snc[snc.index("for i in range(len(blob) // PACKET_BYTES):"):]
    loop = loop[:loop.index("sink.flush()")]
    assert "sink.add_undated(frames)" in loop, "undated audio is still dropped"
    assert "def add_undated" in snc and "def flush_undated" in snc

    # Filed, and honest about how it was placed.
    fn = snc[snc.index("def flush_undated"):]
    fn = fn[:fn.index("\n    def flush(self)")]
    assert "time_known=False" in fn, "it would claim the device said so"
    assert "clipwriter.save_wav" in fn, "counted but never written"

    # Nothing is left holding audio when the spool is done.
    tail = snc[snc.index("os.remove(path)"):]
    assert "sink.flush_undated()" in tail, "the last of it is never filed"

    src = read_file("host/omid.py")
    assert "placed by arrival" in src
