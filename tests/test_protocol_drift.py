"""The two firmwares and the host must agree about the protocol.

Nothing generates these constants from one definition: they are written out
by hand in the Zephyr header, the Arduino sketch, and the host parser. That
has already produced two real bugs -- the capture-state bit one build
published and the other did not, and a backlog mode whose value meant
opposite things depending on which firmware was flashed.

Generating them would be better. This is the cheaper thing that catches the
same drift: read all three, and fail when they disagree.
"""
import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
ZEPHYR = os.path.join(ROOT, "firmware", "zephyr", "boswell", "src", "proto.h")
ARDUINO = os.path.join(ROOT, "firmware", "ble_mic", "ble_mic.ino")
HOST = os.path.join(ROOT, "web", "server.py")


def defines(path, prefix):
    """#define NAME 0x1234 -> {NAME: 4660}"""
    out = {}
    for line in open(path):
        m = re.match(rf"\s*#define\s+({prefix}\w+)\s+(0x[0-9A-Fa-f]+|\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2), 0)
    return out


def enum_values(path, prefix):
    """NAME = 0x01, inside an enum -> {NAME: 1}"""
    out = {}
    for line in open(path):
        m = re.match(rf"\s*({prefix}\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)", line)
        if m:
            out[m.group(1)] = int(m.group(2), 0)
    return out


class TestCapabilityBits:
    def test_both_firmwares_agree(self):
        z = defines(ZEPHYR, "INFO_CAP_")
        a = defines(ARDUINO, "INFO_CAP_")
        assert z, "no capability bits found in the Zephyr header"
        shared = set(z) & set(a)
        assert shared, "the two firmwares share no capability names"
        disagree = {k: (z[k], a[k]) for k in shared if z[k] != a[k]}
        assert not disagree, f"capability bits differ: {disagree}"

    def test_no_two_capabilities_share_a_bit(self):
        z = defines(ZEPHYR, "INFO_CAP_")
        seen = {}
        for name, val in z.items():
            assert val not in seen, f"{name} and {seen[val]} are both {val:#x}"
            seen[val] = name

    def test_the_host_reads_the_bits_the_firmware_publishes(self):
        """The host decodes capabilities with literals; they must match."""
        z = defines(ZEPHYR, "INFO_CAP_")
        src = open(HOST).read()
        for name, val in (("INFO_CAP_STEPS", z.get("INFO_CAP_STEPS")),
                          ("INFO_CAP_OVERRUNS", z.get("INFO_CAP_OVERRUNS")),
                          ("INFO_CAP_STATE", z.get("INFO_CAP_STATE")),
                          ("INFO_CAP_BOOTID", z.get("INFO_CAP_BOOTID"))):
            if val is None:
                continue
            assert f"caps & {val:#06x}" in src or f"caps & {val:#x}" in src, \
                f"the host does not test {name} ({val:#x})"


class TestControlOpcodes:
    def test_no_opcode_is_defined_twice(self):
        z = enum_values(ZEPHYR, "CTRL_")
        assert z, "no control opcodes found"
        seen = {}
        for name, val in z.items():
            assert val not in seen, f"{name} and {seen[val]} are both {val:#x}"
            seen[val] = name

    def test_arduino_handles_the_opcodes_zephyr_defines(self):
        """Not every opcode has to exist on both, but a shared one must match."""
        z = enum_values(ZEPHYR, "CTRL_")
        ino = open(ARDUINO).read()
        # Arduino dispatches on raw hex in a switch.
        for name, val in z.items():
            if name in ("CTRL_IMU_STREAM", "CTRL_IMU_GYRO",
                        "CTRL_BUFFER", "CTRL_REPLAY", "CTRL_OTA"):
                continue          # Zephyr-only features
            assert re.search(rf"case\s+0x{val:02X}\s*:", ino, re.I), \
                f"Arduino does not handle {name} ({val:#04x})"


class TestInfoLayout:
    def test_both_firmwares_publish_the_same_length(self):
        z = re.search(r"info_buf\[(\d+)\]", open(ZEPHYR.replace("proto.h", "ble_audio.c")).read())
        a = re.search(r"uint8_t info\[(\d+)\]", open(ARDUINO).read())
        assert z and a, "could not find both info buffers"
        assert int(z.group(1)) == int(a.group(1)), \
            f"info characteristic is {z.group(1)} bytes on Zephyr, {a.group(1)} on Arduino"
