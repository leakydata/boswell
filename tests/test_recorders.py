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
