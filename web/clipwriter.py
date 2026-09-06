"""Writing one clip into the archive, in one place.

Three paths produce clips now -- the live radio, a docked card, and a second
recorder streaming over Bluetooth -- and each of them was growing its own
copy of "write a WAV, then write the times record beside it". Three copies is
three chances for the rules to drift, and the rules are what let audio be
found again: what time it happened, which device recorded it, whether that
time is a fact or a guess.

So: one function, used by all of them.

Everything here was learned the hard way somewhere else in this project and
is repeated because it is not obvious:

  * A name that cannot collide. Names carried a whole-second timestamp, and
    two clips finalised in the same second silently overwrote one another.
  * A file that is either complete or absent. The WAV went straight to its
    final name, so an interrupted write left a truncated file that looks like
    a recording and indexes like one.
  * The caller keeps its audio until this returns. take_clip() used to empty
    its buffer before writing, so anything that went wrong took the only copy.
"""

import json
import os

import numpy as np
import soundfile as sf

import atomicio
import index_db

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
TIMES = os.path.join(DATA, "times")


def save_wav(prefix, when, audio, rate, data_dir=None):
    """Write one clip and hand back its path."""
    d = data_dir or DATA
    os.makedirs(d, exist_ok=True)

    name = f"{prefix}_{int(when)}"
    path = os.path.join(d, name + ".wav")
    n = 1
    while os.path.exists(path):
        path = os.path.join(d, f"{name}-{n}.wav")
        n += 1

    tmp = path + ".part"
    try:
        # Format stated explicitly: soundfile infers it from the extension,
        # and the temporary name ends in .part.
        sf.write(tmp, audio, rate, format="WAV", subtype="PCM_16")
        with open(tmp, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def write_times(path, *, started, ended, seconds, source, first_ms, last_ms,
                boot_id=None, device_id=None, time_known=True, times_dir=None):
    """Record when the audio happened, according to the device that heard it.

    Written beside the clip so ordering never has to be reconstructed from
    filesystem metadata. mtime can be rewritten by a copy, a backup or a
    sync; the device's own counter cannot.
    """
    t = times_dir or TIMES

    if ended < started:
        ended = started + seconds
    # A stale device counter on one frame can claim a span the audio cannot
    # cover; see index_db.implausible_span for what that broke. The end is
    # the trustworthy edge -- it is when the clip was actually written -- so
    # the start is derived back from it.
    if index_db.implausible_span(started, ended, seconds):
        started = ended - seconds

    os.makedirs(t, exist_ok=True)
    rec = {
        "name": os.path.basename(path),
        "started": round(started, 3),
        "ended": round(ended, 3),
        "seconds": round(seconds, 3),
        "source": source,
        # An uptime counter, so 41,900 ms happens once per boot and says
        # nothing alone. The boot id is what makes it an identity, and the
        # device id is what stops two recorders sharing one.
        "device_ms": [first_ms, last_ms],
        "boot_id": boot_id,
        "device_id": device_id,
        # False when nothing ever told the device the time, so the figures
        # above are a placement of last resort rather than a fact.
        "time_known": bool(time_known),
    }
    try:
        atomicio.write_json(os.path.join(t, os.path.basename(path) + ".json"), rec)
    except Exception:
        pass
    try:
        os.utime(path, (ended, ended))     # keep mtime consistent too
    except OSError:
        pass
    return rec
