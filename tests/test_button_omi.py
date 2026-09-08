"""The button on the Omi.

Their firmware notifies every press and nothing was listening, so "did I turn
it off, or did it drop?" was unanswerable for two days while the device knew
the whole time.
"""
import json
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "host"))
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import omi_capture as oc


def read_file(p):
    with open(os.path.join(HERE, "..", p)) as f:
        return f.read()


def test_the_event_codes_match_their_firmware():
    # omi/src/lib/core/button.c. Guessing these would put the wrong name on
    # the one gesture that means the device just powered itself off.
    assert (oc.TAP_SINGLE, oc.TAP_DOUBLE, oc.TAP_LONG) == (1, 2, 3)
    assert (oc.BTN_PRESS, oc.BTN_RELEASE) == (4, 5)
    assert oc.BUTTON_EVENTS[oc.TAP_LONG] == "long press"
    assert oc.OMI_BUTTON.startswith("23ba7925")


def test_the_button_rides_the_connection_that_already_exists():
    """The radio is exclusive and this recorder advertises in short windows.
    A second connection to read a button would be another search that can
    fail, for something that is a convenience."""
    src = read_file("host/omi_capture.py")
    fn = src[src.index("async def capture("):]
    fn = fn[:fn.index("if on_connected:")]
    # One connection: the button is subscribed on the client the audio
    # stream already opened, not on a second one.
    assert "start_notify(OMI_BUTTON" in fn
    assert "BleakClient(" not in fn[fn.index("start_notify(OMI_AUDIO"):]
    assert "on_button=None" in fn[:400]


def test_a_missing_button_service_never_costs_a_recording():
    # Older firmware may not carry it at all, and a recorder that streams
    # audio but cannot report its button is worth far more than one that
    # refuses to start.
    src = read_file("host/omi_capture.py")
    fn = src[src.index("if on_button:"):]
    fn = fn[:fn.index("# Only now is there a link")]
    assert "except Exception" in fn


def test_the_first_int_of_the_payload_is_the_event():
    # Eight bytes, little-endian, first int is the code.
    src = read_file("host/omi_capture.py")
    fn = src[src.index("def _on_button"):]
    fn = fn[:fn.index("try:")]
    assert 'int.from_bytes(bytes(data[:4]), "little")' in fn
    assert "len(data) < 4" in fn, "a short payload would raise on unpack"


def test_a_press_is_written_down_with_its_time(tmp_path, monkeypatch):
    import omid
    monkeypatch.setattr(omid, "MOMENTS", str(tmp_path / "moments.jsonl"))
    rec = omid.note_moment("double tap", device_id="c4b3fd7f1e91")
    assert rec["kind"] == "double tap" and rec["at"] > 0
    line = json.loads(open(str(tmp_path / "moments.jsonl")).read().strip())
    assert line["kind"] == "double tap"
    assert line["device_id"] == "c4b3fd7f1e91"


def test_press_and_release_are_not_filed_as_gestures():
    """They bracket every tap, so filing them would make three lines out of
    one gesture and bury the gesture."""
    src = read_file("host/omid.py")
    fn = src[src.index("    def on_button(code):"):]
    fn = fn[:fn.index("\n    try:")]
    assert "omi_capture.BTN_PRESS, omi_capture.BTN_RELEASE" in fn
    assert "return" in fn[:fn.index("note_moment")]


def test_a_long_press_is_not_reported_as_a_lost_link():
    """The firmware acts on a long press by shutting the microphone and the
    radio down. Calling that "lost the link" sends somebody chasing a fault
    that is a switch, and the daemon hunts a device that is not coming back.
    """
    src = read_file("host/omid.py")
    fn = src[src.index("    def on_button(code):"):]
    fn = fn[:fn.index("\n    try:")]
    assert "omi_capture.TAP_LONG" in fn
    assert 'state="switched off"' in fn

    loop = src[src.index("async def run(address=None"):]
    assert 'publish(state="switched off", address=addr)' in loop, \
        "a failed session after a long press still reports a fault"


def test_switching_it_back_on_clears_the_flag():
    # Otherwise one long press marks the recorder off for the life of the
    # daemon, and it never reports recording again.
    src = read_file("host/omid.py")
    loop = src[src.index("sighted = await wait_until_advertising"):]
    loop = loop[:loop.index("else:")]
    assert 'switched_off["at"] = None' in loop


def test_a_recorder_switched_off_is_not_a_recorder_connected():
    src = read_file("web/server.py")
    away = src[src.index("_AWAY = "):]
    away = away[:away.index("\n\n\ndef ")]
    assert '"switched off"' in away


def test_the_presses_can_be_read_back():
    src = read_file("web/server.py")
    assert '@app.get("/api/moments")' in src
    fn = src[src.index('@app.get("/api/moments")'):]
    fn = fn[:fn.index("\n@app.")]
    assert "out.reverse()" in fn, "oldest first is the wrong end of a log"
    assert "FileNotFoundError" in fn, "no presses yet is not an error"
