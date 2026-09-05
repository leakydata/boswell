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
