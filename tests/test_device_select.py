"""Choosing between boards that advertise the same name.

Every board running this firmware calls itself XIAO-MIC, so once there were
two on the bench "find the one called XIAO-MIC" had two answers and the host
took whichever won the scan. These cover the matching rules and the failure
message, which is the part that makes a mix-up visible.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "host"))

import ble_capture as bc

ADDR = "E1:23:45:67:89:AB"


def test_unset_keeps_the_old_behaviour():
    assert bc.device_matches(None, ADDR, bc.DEVICE_NAME)
    assert bc.device_matches("", ADDR, bc.DEVICE_NAME)
    assert not bc.device_matches(None, ADDR, "something-else")
    assert not bc.device_matches(None, ADDR, None)


@pytest.mark.parametrize("written", [
    "E1:23:45:67:89:AB",
    "e1:23:45:67:89:ab",
    "e1-23-45-67-89-ab",
    "E123456789AB",
])
def test_an_address_matches_however_it_was_written(written):
    """All four are the same board and all four are things people paste."""
    assert bc.device_matches(written, ADDR, bc.DEVICE_NAME)
    assert bc.device_matches(written, ADDR, "any name at all")


def test_a_different_address_does_not_match():
    assert not bc.device_matches(ADDR, "AA:BB:CC:DD:EE:FF", bc.DEVICE_NAME)
    assert not bc.device_matches(ADDR, None, bc.DEVICE_NAME)


def test_a_non_address_is_treated_as_a_name():
    assert bc.device_matches("boswell-spare", "AA:BB:CC:DD:EE:FF", "boswell-spare")
    assert bc.device_matches("BOSWELL-SPARE", "AA:BB:CC:DD:EE:FF", "boswell-spare")
    assert not bc.device_matches("boswell-spare", ADDR, bc.DEVICE_NAME)


def test_the_env_var_drives_it(monkeypatch):
    monkeypatch.delenv("BOSWELL_DEVICE", raising=False)
    assert bc.wanted_device() is None
    monkeypatch.setenv("BOSWELL_DEVICE", "  ")
    assert bc.wanted_device() is None, "whitespace is not a request"
    monkeypatch.setenv("BOSWELL_DEVICE", f"  {ADDR}  ")
    assert bc.wanted_device() == ADDR


def test_missing_says_what_was_there_instead():
    """The useful thing to report is not that the board was absent but which
    ones were present -- that is what a two-board mix-up looks like."""
    msg = bc.describe_missing(ADDR, [("AA:BB:CC:DD:EE:FF", "XIAO-MIC")])
    assert ADDR in msg
    assert "XIAO-MIC" in msg and "AA:BB:CC:DD:EE:FF" in msg

    assert "nothing was advertising" in bc.describe_missing(None, [])
    assert bc.DEVICE_NAME in bc.describe_missing(None, [])


def test_missing_does_not_dump_the_whole_room():
    seen = [(f"AA:BB:CC:DD:EE:{i:02X}", f"dev{i}") for i in range(20)]
    msg = bc.describe_missing(None, seen)
    assert "+14 more" in msg
    assert msg.count("dev") == 6


# ------------------------------------------------------- USB serial confusion
#
# The board carries two identifiers and neither is labelled on the desk. The
# USB serial is what the bootloader and `lsusb` show; Bluetooth answers to a
# different, shorter one. Pasting the USB serial into BOSWELL_DEVICE used to
# match nothing and report the board simply missing, which reads as a dead
# board rather than a typo.

def test_usb_serial_is_recognised_as_such():
    assert bc.looks_like_usb_serial("A4C0D6ECF3D91437")
    assert bc.looks_like_usb_serial("a4c0d6ecf3d91437")


def test_bluetooth_address_is_not_mistaken_for_a_serial():
    for form in ("D9:66:CF:BB:58:A4", "d9-66-cf-bb-58-a4", "d966cfbb58a4"):
        assert not bc.looks_like_usb_serial(form)


def test_a_name_is_not_mistaken_for_a_serial():
    assert not bc.looks_like_usb_serial("XIAO-MIC")


def test_usb_serial_matches_nothing():
    assert not bc.device_matches(
        "A4C0D6ECF3D91437", "D9:66:CF:BB:58:A4", "XIAO-MIC")


def test_the_address_still_matches_after_the_change():
    for form in ("D9:66:CF:BB:58:A4", "d9-66-cf-bb-58-a4", "d966cfbb58a4"):
        assert bc.device_matches(form, "D9:66:CF:BB:58:A4", "XIAO-MIC")


def test_the_message_says_it_is_a_serial_rather_than_missing():
    msg = bc.describe_missing(
        "A4C0D6ECF3D91437", [("D9:66:CF:BB:58:A4", "XIAO-MIC")])
    assert "USB serial" in msg
    assert "not found" not in msg          # it was found; it was asked for wrongly
    assert "D9:66:CF:BB:58:A4" in msg      # and here is the one to use


def test_the_serial_message_survives_an_empty_scan():
    msg = bc.describe_missing("A4C0D6ECF3D91437", [])
    assert "USB serial" in msg


# ------------------------------------------------------- the rename
#
# The board advertised XIAO-MIC, which is the name of the module it is built
# on rather than the name of the thing. Renaming it is a two-sided change: a
# host that stops recognising the old name reports the device missing while
# it sits there advertising, and a board that has not been reflashed yet is
# the ordinary case for as long as the rename takes.

def test_the_new_name_is_matched():
    assert bc.DEVICE_NAME == "Boswell"
    assert bc.device_matches(None, ADDR, "Boswell")


def test_the_old_name_is_still_matched():
    assert bc.device_matches(None, ADDR, "XIAO-MIC")


def test_something_else_is_not():
    assert not bc.device_matches(None, ADDR, "Omi")
    assert not bc.device_matches(None, ADDR, "")


def test_the_firmwares_advertise_the_new_name():
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    conf = open(os.path.join(root, "firmware", "zephyr", "boswell",
                             "prj.conf")).read()
    assert 'CONFIG_BT_DEVICE_NAME="Boswell"' in conf
    ino = open(os.path.join(root, "firmware", "ble_mic", "ble_mic.ino")).read()
    assert 'Bluefruit.setName("Boswell")' in ino
