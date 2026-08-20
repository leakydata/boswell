"""Firmware logic compiled and tested on this machine.

Every firmware fault this project has found needed a build, a flash, a
reconnect and a shell session before anything could be observed -- the
backlog jam took two rounds of instrumentation on hardware to locate. The
parts that are pure arithmetic do not have to be like that.

The C is compiled from the same header the firmware includes, not
reimplemented in Python. A reimplementation tests the reimplementation.
"""
import ctypes
import os
import shutil
import subprocess
import tempfile

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..",
                   "firmware", "zephyr", "boswell", "src")

pytestmark = pytest.mark.skipif(shutil.which("gcc") is None,
                                reason="gcc not available")


@pytest.fixture(scope="module")
def fw():
    """Compile the firmware's own CRC into a shared library and load it."""
    tmp = tempfile.mkdtemp()
    shim = os.path.join(tmp, "shim.c")
    with open(shim, "w") as f:
        f.write('#include "rec_crc.h"\n'
                'uint8_t crc(const uint8_t *d, uint8_t n) { return rec_crc8(d, n); }\n')
    lib = os.path.join(tmp, "fw.so")
    subprocess.run(["gcc", "-shared", "-fPIC", "-O2",
                    f"-I{os.path.abspath(SRC)}", shim, "-o", lib], check=True)
    dll = ctypes.CDLL(lib)
    dll.crc.restype = ctypes.c_uint8
    dll.crc.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint8]
    return dll


def _crc(fw, data: bytes) -> int:
    buf = (ctypes.c_uint8 * len(data))(*data)
    return fw.crc(buf, len(data))


class TestRecordCrc:
    def test_stable_for_the_same_bytes(self, fw):
        assert _crc(fw, b"hello world") == _crc(fw, b"hello world")

    def test_differs_for_different_bytes(self, fw):
        assert _crc(fw, b"hello world") != _crc(fw, b"hello worlD")

    def test_catches_every_single_bit_flip(self, fw):
        """The case this exists for: a payload read from the wrong offset."""
        base = bytes(range(64))
        want = _crc(fw, base)
        missed = 0
        for i in range(len(base)):
            for bit in range(8):
                bad = bytearray(base)
                bad[i] ^= (1 << bit)
                if _crc(fw, bytes(bad)) == want:
                    missed += 1
        assert missed == 0, f"{missed} single-bit flips went undetected"

    def test_length_matters(self, fw):
        assert _crc(fw, b"\x00") != _crc(fw, b"\x00\x00")

    def test_empty_payload(self, fw):
        assert _crc(fw, b"") == 0xFF        # the initial value, untouched

    @pytest.mark.parametrize("n", [1, 2, 3, 199, 200])
    def test_runs_over_the_whole_payload_range(self, fw, n):
        data = bytes((i * 7 + 3) & 0xFF for i in range(n))
        assert 0 <= _crc(fw, data) <= 255


class TestMisalignedRecordDetection:
    """A cursor landing mid-record is what the CRC is guarding against."""

    def test_a_shifted_read_is_rejected(self, fw):
        payload = bytes((i * 13 + 5) & 0xFF for i in range(160))
        good = _crc(fw, payload)
        # Read starting one byte late, as a misaligned cursor would.
        shifted = payload[1:] + b"\x00"
        assert _crc(fw, shifted) != good

    def test_a_plausible_but_wrong_record_is_rejected(self, fw):
        """Magic and a believable length can both agree by coincidence; the
        payload is what settles it."""
        real = bytes(range(100))
        coincidence = bytes(range(1, 101))
        assert _crc(fw, real) != _crc(fw, coincidence)
