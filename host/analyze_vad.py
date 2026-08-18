#!/usr/bin/env python3
"""
Pick a VAD threshold from a real recording instead of guessing.

Computes per-frame RMS over 20 ms frames (matching the firmware's frame size),
finds the split between the silence and speech modes with Otsu's method on
log-RMS, and reports the firmware control value to write.

Usage:
    uv run host/analyze_vad.py data/vad_sample.wav
"""

import argparse
import sys

import numpy as np
import soundfile as sf

FRAME_MS = 20


def otsu(values, bins=128):
    """Otsu's threshold on log-RMS. Speech/silence is close to bimodal in the
    log domain but badly skewed in the linear one, hence the log."""
    hist, edges = np.histogram(values, bins=bins)
    total = hist.sum()
    if total == 0:
        return None
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = total - w0
    valid = (w0 > 0) & (w1 > 0)
    if not valid.any():
        return None
    csum = np.cumsum(hist * centers)
    m0 = np.divide(csum, w0, out=np.zeros_like(csum), where=w0 > 0)
    m1 = np.divide(csum[-1] - csum, w1, out=np.zeros_like(csum), where=w1 > 0)
    between = w0 * w1 * (m0 - m1) ** 2
    between[~valid] = -1
    return float(centers[int(np.argmax(between))])


def sparkline(counts, height=8):
    blocks = " ▁▂▃▄▅▆▇█"
    mx = counts.max() if counts.max() else 1
    return "".join(blocks[min(8, int(round(c / mx * 8)))] for c in counts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--margin-db", type=float, default=3.0,
                    help="raise threshold above the Otsu split for safety")
    args = ap.parse_args()

    audio, rate = sf.read(args.wav, dtype="int16")
    if audio.ndim > 1:
        audio = audio[:, 0]

    n = int(rate * FRAME_MS / 1000)
    usable = len(audio) // n * n
    frames = audio[:usable].astype(np.float64).reshape(-1, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    rms = np.maximum(rms, 1e-3)
    logr = np.log10(rms)

    split_log = otsu(logr)
    if split_log is None:
        print("could not find a split", file=sys.stderr)
        return 1
    split = 10 ** split_log
    threshold = split * (10 ** (args.margin_db / 20.0))

    silence = rms[rms < threshold]
    speech = rms[rms >= threshold]
    pct_gated = 100.0 * len(silence) / len(rms)

    print(f"file            {args.wav}")
    print(f"  {rate} Hz, {len(audio)/rate:.1f}s, {len(rms)} frames of {FRAME_MS}ms")
    print()

    counts, edges = np.histogram(logr, bins=48)
    print("  frame-RMS distribution (log scale)")
    print(f"    {sparkline(counts)}")
    print(f"    {10**edges[0]:.0f}{' ' * 38}{10**edges[-1]:.0f}")
    print()

    print(f"  silence mode   rms ~{np.median(silence) if len(silence) else 0:.0f}")
    print(f"  speech  mode   rms ~{np.median(speech) if len(speech) else 0:.0f}")
    if len(silence) and len(speech):
        snr = 20 * np.log10(np.median(speech) / max(np.median(silence), 1e-3))
        print(f"  separation     {snr:.1f} dB")
    print()
    print(f"  otsu split     {split:.0f}")
    print(f"  + {args.margin_db:.0f} dB margin  -> THRESHOLD {threshold:.0f}")
    print()
    print(f"  would gate     {pct_gated:.1f}% of frames as silence")
    print(f"  duty cycle     {100 - pct_gated:.1f}%")
    print(f"  airtime        35.2 -> {35.2 * (100 - pct_gated) / 100:.1f} kbps average")
    print()

    ctrl = int(round(threshold / 32))
    ctrl = max(1, min(255, ctrl))
    print(f"  firmware control: 0x05 {ctrl}   (threshold = {ctrl} * 32 = {ctrl*32})")
    print(f"  run:  uv run host/ble_capture.py --vad --vad-threshold {ctrl} ...")

    if len(speech) == 0:
        print("\n  ! no speech detected — was anyone talking?")
    elif pct_gated < 5:
        print("\n  ! almost nothing gated; recording is probably continuous speech/noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
