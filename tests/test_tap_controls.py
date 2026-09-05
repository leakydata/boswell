"""Tap settings from the app instead of a serial cable.

CTRL_TAP_ENABLE and CTRL_TAP_THRESH have been in the firmware since the tap
existed; nothing ever sent them, so a board that toggled itself could only be
calmed by reflashing it or unplugging it.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

HERE = os.path.dirname(__file__)
SERVER = os.path.join(HERE, "..", "web", "server.py")
PROTO = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src", "proto.h")
BLE_AUDIO = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src", "ble_audio.c")


def read(p):
    with open(p) as f:
        return f.read()


def test_the_host_sends_the_opcodes_the_firmware_defines():
    proto, server = read(PROTO), read(SERVER)
    for name, op in (("CTRL_TAP_ENABLE", "0x06"), ("CTRL_TAP_THRESH", "0x07")):
        assert re.search(rf"{name}\s*=\s*{op}", proto), f"{name} moved"
        assert f"self._ctrl({op}," in server, f"nothing sends {name}"


def test_both_are_asserted_on_connect():
    """Tap settings live in the board's flash, so a replacement board arrives
    at the shipped default however carefully the last one was tuned."""
    s = read(SERVER)
    block = s[s.index("for op, key, default in ("):]
    block = block[:block.index("await c.start_notify")]
    assert '(0x06, "tap_enabled"' in block
    assert '(0x07, "tap_thresh"' in block


def test_both_survive_a_restart():
    s = read(SERVER)
    keys = s[s.index("PREF_KEYS = ("):]
    keys = keys[:keys.index(")")]
    assert "tap_enabled" in keys and "tap_thresh" in keys


def test_the_threshold_is_published_so_the_ui_can_show_the_truth():
    """Without this the app could only show what it last asked for, which is
    wrong on any board it did not tune itself."""
    assert "INFO_CAP_TAPCFG" in read(PROTO)
    assert "info_buf[45] = imu_tap_get_threshold();" in read(BLE_AUDIO)
    assert 'out["tap_thresh"] = info[45]' in read(SERVER)


def test_the_threshold_is_clamped_to_the_register():
    """TAP_THS_6D is five bits. A value outside it would be truncated by the
    device into something nobody asked for."""
    s = read(SERVER)
    body = s[s.index("async def set_tap_threshold"):]
    body = body[:body.index("async def set_led")]
    assert "max(0, min(31," in body


def test_the_websocket_accepts_both():
    s = read(SERVER)
    assert 'cmd == "tap_enabled"' in s
    assert 'cmd == "tap_thresh"' in s
