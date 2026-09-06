"""Where the models run, decided once.

`"cuda"` was written into seven places in the transcription pipeline, so on a
machine without an NVIDIA card the software did not degrade -- it raised on
the first clip and never recorded a word. That is the wrong failure for a
project somebody else is meant to be able to run: most people who own a
wearable recorder do not own a discrete NVIDIA GPU, and a Mac has none at all.

So the device is chosen here, once, and everything asks. CUDA if it is there,
Apple's MPS if that is, otherwise the CPU.

**The CPU path is slow, and it is meant to be used knowing that.** Measured on
this machine -- Xeon E5-2630 v4, 20 cores at 2.2 GHz, one 30-second clip,
nothing else running:

                             GPU (RTX 4090)      CPU
    whisper large-v3            29.2x           0.79x
    whisper distil-large-v3       --            2.64x
    pyannote diarization        32.0x           0.47x
    AST sound tagger            95.1x          19.0x

Whisper is not the problem. **Diarization is:** 63.7 s to work out who spoke
in 30 s of audio, which is slower than every other stage put together. The
whole CPU pipeline on this machine comes to roughly 0.4x realtime, so a
recorder worn continuously produces audio about two and a half times faster
than the machine can process it, and the backlog never comes back.

That is not a reason to refuse to run. It is a reason to say what it is for:
catching up on a few hours overnight, or a machine that records for part of a
day rather than all of it. A 2026 laptop CPU is several times this 2016 server
part per core, so the ratio there will be better -- and still nothing like a
GPU.

distil-large-v3 is the CPU default anyway. It is three times faster than
large-v3 for a small accuracy cost, and on the CPU path every stage is
competing for the same cores.

Override any of it:

    BOSWELL_TORCH_DEVICE=cpu     force the device (cuda | mps | cpu)
    BOSWELL_ASR_MODEL=medium     force the Whisper model
    BOSWELL_COMPUTE_TYPE=int8    force the precision
"""

import os


def _pick_device():
    forced = (os.environ.get("BOSWELL_TORCH_DEVICE") or "").strip().lower()
    if forced:
        return forced
    try:
        import torch
    except Exception:
        return "cpu"
    try:
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    try:
        # Apple silicon. Present but unexercised here -- no Mac to test on --
        # so it is offered and not promised.
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


DEVICE = _pick_device()

# faster-whisper's own precision names, which are not torch dtypes. float16 is
# the fast path on a modern NVIDIA card; int8 is what makes the CPU bearable.
COMPUTE_TYPE = (os.environ.get("BOSWELL_COMPUTE_TYPE")
                or ("float16" if DEVICE == "cuda" else "int8"))

# Whisper runs through CTranslate2, which has no MPS backend: on a Mac the
# transcriber falls back to the CPU while diarization and tagging can still
# use the GPU. Saying so beats a stack trace from inside a library.
ASR_DEVICE = "cpu" if DEVICE == "mps" else DEVICE

ASR_MODEL = (os.environ.get("BOSWELL_ASR_MODEL")
             or ("large-v3" if DEVICE == "cuda" else "distil-large-v3"))


def threads():
    """How many CPU threads the ASR may use. All of them, on the CPU path --
    it is the whole budget there and nothing else is competing for it."""
    return (os.cpu_count() or 4) if ASR_DEVICE == "cpu" else 4


def describe():
    """One line for the log, so which path is running is never a guess."""
    if DEVICE == "cuda":
        return f"cuda · {ASR_MODEL} · {COMPUTE_TYPE}"
    where = "cpu" if DEVICE == "cpu" else f"{DEVICE} (whisper on cpu)"
    return (f"{where} · {ASR_MODEL} · {COMPUTE_TYPE} · {threads()} threads"
            " — expect transcription to run near real time, not ahead of it")
