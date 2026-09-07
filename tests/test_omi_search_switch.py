"""The switch that stops the daemon hunting for the Omi.

The fault this answers: the only way to free the radio was to stop the whole
daemon, which also stopped it recording, and nothing on screen could do
either. Every case here is one the switch has to get right for that to be a
switch rather than a suggestion.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import omid


@pytest.fixture
def control(tmp_path, monkeypatch):
    """Point the daemon at a throwaway control file, cache cleared."""
    p = tmp_path / "omi_control.json"
    monkeypatch.setattr(omid, "CONTROL", str(p))

    def write(obj):
        # The cache exists so a tight loop does not stat the file every
        # millisecond; it must never be the reason a test sees a stale answer.
        omid._SEARCH.update(at=0.0, on=True)
        if obj is None:
            if p.exists():
                p.unlink()
        else:
            p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    return write


class TestSearching:
    def test_no_file_means_look(self, control):
        """A fresh install has no control file and must still record."""
        control(None)
        assert omid.searching() is True

    def test_off_means_off(self, control):
        control({"search": False})
        assert omid.searching() is False

    def test_on_means_on(self, control):
        control({"search": True})
        assert omid.searching() is True

    @pytest.mark.parametrize("junk", ["", "{", "null", "[]", '"nope"', "3"])
    def test_unreadable_falls_towards_recording(self, control, junk):
        """A corrupt switch must not silently stop the recorder.

        Failing the other way loses audio nobody asked to lose, and looks
        exactly like the device being out of range.
        """
        control(junk)
        assert omid.searching() is True

    def test_a_missing_key_is_not_a_no(self, control):
        control({"at": 123})
        assert omid.searching() is True

    def test_it_can_be_turned_back_on(self, control):
        control({"search": False})
        assert omid.searching() is False
        control({"search": True})
        assert omid.searching() is True

    def test_the_switch_stands_rather_than_being_consumed(self, control):
        """Unlike a settings request, reading it must not clear it.

        A switch that turns itself back on after one reading is not a switch,
        and this is the whole difference between this file and omi_wanted.
        """
        p = control({"search": False})
        for _ in range(3):
            omid._SEARCH.update(at=0.0, on=True)
            assert omid.searching() is False
        assert p.exists()
        assert json.loads(p.read_text())["search"] is False


class TestServerAgrees:
    """The panel and the daemon must not disagree about the switch."""

    def test_same_answer_from_the_web_side(self, tmp_path, monkeypatch):
        import server
        monkeypatch.setattr(server, "DATA", str(tmp_path))
        assert server._omi_searching() is True

        (tmp_path / "omi_control.json").write_text('{"search": false}')
        assert server._omi_searching() is False

        (tmp_path / "omi_control.json").write_text("{oops")
        assert server._omi_searching() is True


class TestTheWaitIsInterruptible:
    """"Stop looking" that leaves a scanner running for a minute is not one.

    The wait between attempts is an active BLE scan, so it is precisely the
    thing somebody is asking to stop when they want the radio back.
    """

    def test_it_does_not_even_open_a_scanner_when_off(self, control,
                                                      monkeypatch):
        import asyncio

        control({"search": False})
        opened = []

        class Boom:
            def __init__(self, *a, **k):
                opened.append(1)

        monkeypatch.setattr(omid.omi_capture, "bluez_device",
                            lambda a: _async(None))
        monkeypatch.setitem(sys.modules, "bleak",
                            type(sys)("bleak"))
        sys.modules["bleak"].BleakScanner = Boom

        got = asyncio.run(omid.wait_until_advertising("AA:BB:CC:DD:EE:FF", 30))
        assert got is None
        assert opened == [], "a scan was started despite the switch being off"

    def test_it_gives_up_early_when_the_switch_goes_off(self, control,
                                                        monkeypatch):
        """A 30 s wait must end in about a second, not in thirty."""
        import asyncio
        import time as _t

        control({"search": True})
        stopped = []

        class Scanner:
            def __init__(self, detection_callback=None):
                pass

            async def start(self):
                pass

            async def stop(self):
                stopped.append(1)

        monkeypatch.setattr(omid.omi_capture, "bluez_device",
                            lambda a: _async(None))
        monkeypatch.setitem(sys.modules, "bleak", type(sys)("bleak"))
        sys.modules["bleak"].BleakScanner = Scanner

        async def drive():
            async def flip():
                await asyncio.sleep(0.2)
                control({"search": False})
            task = asyncio.create_task(flip())
            began = _t.monotonic()
            got = await omid.wait_until_advertising("AA:BB:CC:DD:EE:FF", 30)
            await task
            return got, _t.monotonic() - began

        got, took = asyncio.run(drive())
        assert got is None
        assert took < 3, f"kept scanning for {took:.1f}s after the switch went off"
        assert stopped, "the scanner was left running"


async def _async(v):
    return v
