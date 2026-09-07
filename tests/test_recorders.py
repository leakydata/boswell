"""Which recorders an install has.

The Device tab used to draw a fixed pair of panels -- one for a handmade
board, one for an Omi -- both always there. Right for the person who built
both, wrong for everybody else: somebody who owns an Omi and nothing else
met a large panel about hardware they will never have, with a Connect
button that could not succeed. These cover the list that replaced it.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import recorders


def _fresh(tmp):
    recorders.PATH = os.path.join(tmp, "recorders.json")
    recorders.DATA = tmp
    return recorders


def test_an_address_reduces_to_what_the_archive_stamps_on_clips():
    # The clips carry a bare hex id; the scanner hands back a colon form.
    # A list that could not match them would pair a device already paired.
    assert recorders.norm_id("C4:B3:FD:7F:1E:91") == "c4b3fd7f1e91"
    assert recorders.norm_id("c4b3fd7f1e91") == "c4b3fd7f1e91"
    assert recorders.norm_id(None) is None
    assert recorders.norm_id("") is None


def test_an_existing_archive_does_not_start_with_no_recorders():
    """An install with clips in it already knows what recorded them.

    Starting empty would ask somebody who has been wearing a device for a
    month to pair it before their own page would draw anything.
    """
    tmp = tempfile.mkdtemp()
    r = _fresh(tmp)
    seeded = r.load(seed=lambda: [{"id": "d966cfbb58a4", "kind": "boswell",
                                   "address": None, "name": "Boswell",
                                   "added": 1.0}])
    assert [x["id"] for x in seeded] == ["d966cfbb58a4"]
    # Written down, so the seed runs once and a later forget is not undone
    # by the next read.
    assert os.path.exists(r.PATH)
    r.remove("d966cfbb58a4")
    assert r.load(seed=lambda: [{"id": "d966cfbb58a4", "kind": "boswell",
                                 "address": None, "name": "x", "added": 1.0}]) == []


def test_pairing_the_same_device_twice_does_not_list_it_twice():
    # Re-pairing after a board swap is ordinary; two panels for one device
    # would be worse than a stale name.
    tmp = tempfile.mkdtemp()
    r = _fresh(tmp)
    r.load(seed=list)
    r.add("boswell", "D9:66:CF:BB:58:A4", "Boswell")
    rows = r.add("boswell", "d9:66:cf:bb:58:a4", "Boswell II")
    assert len(rows) == 1
    assert rows[0]["name"] == "Boswell II"


def test_a_recorder_needs_a_kind_and_an_address():
    tmp = tempfile.mkdtemp()
    r = _fresh(tmp)
    r.load(seed=list)
    for bad in (("laptop", "aa:bb:cc:dd:ee:ff"), ("omi", ""), ("omi", None)):
        try:
            r.add(*bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_forgetting_a_recorder_keeps_its_recordings():
    # Forgetting is "this is not one of my devices any more", which is not
    # the same as throwing away what it heard -- and the second is not a
    # thing anyone should do by accident from a page about hardware.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    body = src[src.index("async def api_recorders_forget"):]
    body = body[:body.index("@app.")] if "@app." in body else body
    for destructive in ("remove_clip", "os.remove", "DELETE FROM clips"):
        assert destructive not in body, f"forget touches {destructive}"


def test_the_page_stops_hunting_for_a_recorder_nobody_paired():
    # Otherwise an Omi owner has a radio scanning for a handmade board every
    # two seconds and a panel reporting that it cannot be found.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert 'if not recorders.has("boswell")' in src


def test_one_scan_at_a_time():
    # BlueZ runs a single discovery and refuses the second outright, so a
    # pairing scan landing on top of the reconnect loop simply failed.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert src.count("async with SCAN_LOCK:") >= 2


# ------------------------------------------------------ where models run
def test_the_device_is_decided_in_one_place():
    """`"cuda"` was written into seven calls in the pipeline, so a machine
    without an NVIDIA card did not degrade -- it raised on the first clip and
    never recorded a word."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "pipeline.py")).read()
    assert '"cuda"' not in src, "a hardcoded device is back in the pipeline"
    assert "import compute" in src


def test_the_cpu_path_does_not_default_to_the_slowest_model():
    # large-v3 on this CPU is 0.79x realtime: below 1x a continuous recorder
    # outruns the machine and the backlog never comes back.
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
    os.environ["BOSWELL_TORCH_DEVICE"] = "cpu"
    try:
        import compute
        importlib.reload(compute)
        assert compute.DEVICE == "cpu"
        assert compute.ASR_MODEL == "distil-large-v3"
        assert compute.COMPUTE_TYPE == "int8"
        assert compute.threads() > 4
    finally:
        del os.environ["BOSWELL_TORCH_DEVICE"]
        import compute
        importlib.reload(compute)


def test_whisper_falls_off_mps_because_ctranslate2_has_no_backend():
    # Otherwise a Mac gets a stack trace from inside a library rather than a
    # working transcriber on the CPU.
    import importlib
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
    os.environ["BOSWELL_TORCH_DEVICE"] = "mps"
    try:
        import compute
        importlib.reload(compute)
        assert compute.DEVICE == "mps"
        assert compute.ASR_DEVICE == "cpu"
    finally:
        del os.environ["BOSWELL_TORCH_DEVICE"]
        import compute
        importlib.reload(compute)


def test_the_header_only_names_recorders_this_install_has():
    """`recorders` has one entry per kind the code can talk to, which is not
    one per device somebody owns. A fresh install with nothing paired was
    headed "Omi - paused" in red, naming hardware the reader may never have
    seen."""
    html = open(os.path.join(os.path.dirname(__file__), "..",
                             "web", "static", "index.html")).read()
    assert "function ourRecorders()" in html
    assert "no recorder paired" in html
    # And it must not read the list before it has been fetched: empty and
    # not-yet-asked are the same array, and confusing them flashes "no
    # recorder paired" over a working setup on every load.
    assert "pairedLoaded" in html


def test_a_lost_link_is_not_a_connected_recorder():
    # "lost" and "waiting" are the daemon saying the link went. Counting them
    # as connected had the header calling an absent recorder paused.
    html = open(os.path.join(os.path.dirname(__file__), "..",
                             "web", "static", "index.html")).read()
    block = html[html.index("connected: !!(s && s.running"):][:400]
    for word in ("lost", "waiting", "not found", "looking", "stopped"):
        assert f'"{word}"' in block, f"{word} still counts as connected"


def test_a_live_daemon_is_not_a_connected_device():
    """The diagnosis panel reported a recorder as connected two hours after
    it stopped recording, because the check asked whether the daemon was
    alive. It was -- it was alive and failing to find the device every 25
    seconds, which keeps the status file fresh."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert "_omi_connected" in src
    block = src[src.index("_AWAY = "):src.index("_AWAY = ") + 200]
    for word in ("lost", "waiting", "not found", "stopped"):
        assert f'"{word}"' in block, f"{word} still counts as connected"
    # And the ambiguous helper is gone rather than left as a footgun.
    assert "_omi_running" not in src


def test_the_diagnosis_separates_the_three_faults():
    # "your Bluetooth is off", "the device is off" and "your phone is holding
    # it" have completely different fixes and only one is this program's.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    body = src[src.index("async def api_recorders_diagnose"):]
    body = body[:body.index("@app.post")]
    assert "bluetoothctl" in body, "no adapter check"
    assert "nearby but not connected" in body, "advertising is not told apart"
    assert "not advertising" in body
    assert "last_clip" in body, "no answer to how long it has been gone"


def test_disconnect_is_remembered():
    """Pressing Disconnect and finding the machine hunting for the device a
    minute later -- because the service restarted -- is the interface
    overruling a decision somebody made on purpose."""
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    keys = src[src.index("PREF_KEYS"):src.index("PREF_KEYS") + 400]
    assert "connect_wanted" in keys, \
        "the choice is not among the remembered preferences"
    assert 'PREFS.get("connect_wanted", True)' in src, "startup ignores it"
    assert "remember=False" in src, "the program's own disconnects are stored"


def test_a_recorder_nobody_is_looking_for_is_not_a_fault():
    # An alert that fires on request is one nobody reads.
    src = open(os.path.join(os.path.dirname(__file__), "..",
                            "web", "server.py")).read()
    assert "not being looked for" in src
