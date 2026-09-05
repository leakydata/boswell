"""The Opus path, end to end on the host side.

The firmware half cannot be exercised here, but everything the host does
with an Opus frame can be: that the codec flag routes to the right decoder,
that the ADPCM length guard does not reject a compressed payload, and that
audio survives the round trip well enough to transcribe.
"""
import os
import sys

import numpy as np
import pytest

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "host"),
                os.path.join(os.path.dirname(__file__), "..", "web")]

import ble_capture as bc

FLAG_16K = 0x01
FLAG_OPUS = 0x10


def _libopus():
    try:
        return bc._opus_lib()
    except Exception:
        return None


needs_opus = pytest.mark.skipif(_libopus() is None,
                                reason="libopus not installed")


class _Encoder:
    """One encoder, reused across frames -- the firmware keeps its encoder for
    the life of the session, and a fresh one per frame would encode every
    frame as though it followed silence."""

    def __init__(self, rate):
        import ctypes
        lib = bc._opus_lib()
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16),
                                    ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
        err = ctypes.c_int()
        self.lib = lib
        self.enc = lib.opus_encoder_create(rate, 1, 2051, ctypes.byref(err))
        assert self.enc and err.value == 0

    def __call__(self, pcm):
        import ctypes
        out = ctypes.create_string_buffer(160)
        buf = (ctypes.c_int16 * len(pcm))(*pcm.tolist())
        n = self.lib.opus_encode(self.enc, buf, len(pcm), out, len(out))
        assert n > 0
        return out.raw[:n]


def _tone(n, rate, hz=440.0, phase=0.0):
    t = np.arange(n) / float(rate) + phase
    return (np.sin(2 * np.pi * hz * t) * 8000).astype(np.int16)


def _dominant_hz(pcm, rate):
    win = np.hanning(len(pcm))
    mag = np.abs(np.fft.rfft(pcm.astype(float) * win))
    return np.fft.rfftfreq(len(pcm), 1.0 / rate)[int(np.argmax(mag))]


@needs_opus
@pytest.mark.parametrize("rate,count", [(16000, 320), (8000, 160)])
def test_round_trip_preserves_the_signal(rate, count):
    """20 ms frames at either rate, run as a continuous stream.

    Not a sample-by-sample comparison: CELT carries roughly 2.5 ms of
    algorithmic delay, which at 440 Hz is more than a whole period, so the
    output is phase-shifted by an arbitrary amount and correlating against
    the input measures the delay rather than the quality. What has to survive
    is the content -- so this checks the tone comes back at the frequency it
    went in at, with its energy intact.
    """
    bc._OPUS_DECODERS.clear()
    enc = _Encoder(rate)
    flags = FLAG_OPUS | (FLAG_16K if rate == 16000 else 0)

    frames_in, frames_out = [], []
    for i in range(25):                       # half a second
        pcm = _tone(count, rate, phase=i * count / float(rate))
        out = bc.decode_frame(enc(pcm), flags, 0, 0, count)
        assert len(out) == count
        assert out.dtype == np.int16
        frames_in.append(pcm)
        frames_out.append(out)

    # Skip the first few frames: the codec is priming and the decoder is
    # filling its overlap buffer, so early output is legitimately quiet.
    a = np.concatenate(frames_in[5:])
    b = np.concatenate(frames_out[5:])

    got = _dominant_hz(b, rate)
    assert abs(got - 440.0) < 25, f"tone came back at {got:.0f} Hz"

    rms_in = np.sqrt(np.mean(a.astype(float) ** 2))
    rms_out = np.sqrt(np.mean(b.astype(float) ** 2))
    assert 0.5 < rms_out / rms_in < 2.0, f"energy {rms_out / rms_in:.2f}x"


@needs_opus
def test_compressed_payload_is_not_judged_by_adpcm_length():
    """An Opus frame is far shorter than nsamples/2. The guard that protects
    ADPCM from a truncated payload would drop every one of them."""
    payload = _Encoder(16000)(_tone(320, 16000))
    assert len(payload) < 320 // 2          # the whole point
    assert bc.payload_is_complete(payload, FLAG_OPUS | FLAG_16K, 320)
    assert not bc.payload_is_complete(b"", FLAG_OPUS | FLAG_16K, 320)


def test_adpcm_still_routes_to_adpcm():
    """Frames without the flag must decode exactly as before -- every
    existing recording in the archive is one of these."""
    pcm = _tone(320, 16000)
    nibbles = bytes(160)
    expected = bc.decode_block(nibbles, 0, 0, 320)
    got = bc.decode_frame(nibbles, FLAG_16K, 0, 0, 320)
    assert np.array_equal(expected, got)
    assert bc.payload_is_complete(nibbles, FLAG_16K, 320)
    assert not bc.payload_is_complete(bytes(159), FLAG_16K, 320)
