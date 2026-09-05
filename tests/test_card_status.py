"""The card, visible from the app instead of a serial cable.

The card is the whole point of the second build, and until now the only way
to know whether it had mounted was to open a shell on the device.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import server  # noqa: E402  (path set above)

HERE = os.path.dirname(__file__)
UI = os.path.join(HERE, "..", "web", "static", "index.html")
PROTO = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src", "proto.h")
ARDUINO = os.path.join(HERE, "..", "firmware", "ble_mic", "ble_mic.ino")


def info(caps, **fields):
    """An info payload with the capability bits set and the card bytes filled."""
    buf = bytearray(51)
    buf[20] = caps & 0xFF
    buf[21] = (caps >> 8) & 0xFF
    buf[46] = 1 if fields.get("present") else 0
    total = fields.get("total_mb", 0)
    free = fields.get("free_mb", 0)
    buf[47], buf[48] = total & 0xFF, (total >> 8) & 0xFF
    buf[49], buf[50] = free & 0xFF, (free >> 8) & 0xFF
    return bytes(buf)


def caps_offset_is_where_we_think():
    """Guard the assumption the other tests rest on."""
    parsed = server.parse_info(info(0x1000, present=True, total_mb=1, free_mb=1))
    return parsed.get("card_present") is True


def test_the_capability_bit_is_what_makes_the_bytes_mean_anything():
    assert caps_offset_is_where_we_think(), "caps are not at bytes 20-21"

    with_card = server.parse_info(info(0x1000, present=True,
                                       total_mb=14893, free_mb=14892))
    assert with_card["card_present"] is True
    assert with_card["card_total_mb"] == 14893
    assert with_card["card_free_mb"] == 14892


def test_no_capability_means_unknown_not_absent():
    """A firmware without card support and a board with an empty slot report
    the same zeroes. Only the bit tells them apart, and reporting 'no card'
    for the first sends somebody hunting a fault that is not there."""
    parsed = server.parse_info(info(0x0000))
    assert parsed["card_present"] is None


def test_supported_but_empty_is_false_not_none():
    parsed = server.parse_info(info(0x1000, present=False))
    assert parsed["card_present"] is False


def test_a_short_payload_does_not_claim_a_card():
    """Older firmware sends a shorter characteristic; reading past it would
    be whatever bleak left in the buffer."""
    short = info(0x1000, present=True, total_mb=99, free_mb=9)[:46]
    assert server.parse_info(short)["card_present"] is None


def test_the_arduino_build_zero_fills_the_new_bytes():
    """Nothing memsets that array, so residue in a free-space field reads as
    a card with an arbitrary amount of room."""
    src = open(ARDUINO).read()
    for byte in range(46, 51):
        assert re.search(rf"info\[{byte}\]\s*=\s*0", src), f"info[{byte}] unfilled"


def test_the_capability_bit_is_declared_once():
    assert "INFO_CAP_SDCARD    0x1000" in open(PROTO).read()


def test_the_ui_distinguishes_all_three_states():
    ui = open(UI).read()
    assert "function cardLabel" in ui
    assert "not supported by this firmware" in ui
    assert '"no card"' in ui


# --------------------------------------------------------------- codec id

def test_the_status_line_says_which_codec_the_device_is_using():
    """The label was hardcoded to ADPCM, so a board encoding Opus still
    reported ADPCM in the status line -- and the firmware had been publishing
    the real value in info byte 0 the whole time with nothing reading it."""
    buf = bytearray(info(0x1000))
    buf[0] = 1
    assert server.parse_info(bytes(buf))["codec_name"] == "ADPCM"
    buf[0] = 20
    assert server.parse_info(bytes(buf))["codec_name"] == "Opus"


def test_an_unknown_codec_is_reported_not_guessed():
    """A firmware newer than this host should say so rather than be labelled
    with whichever codec happened to be in the dictionary."""
    buf = bytearray(info(0x1000))
    buf[0] = 77
    parsed = server.parse_info(bytes(buf))
    assert parsed["codec"] == 77
    assert "77" in parsed["codec_name"]


def test_the_ui_no_longer_hardcodes_a_codec():
    ui = open(UI).read()
    assert "s.codec_name" in ui, "the status line still ignores the device"
