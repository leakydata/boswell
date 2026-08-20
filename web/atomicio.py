"""
Write files so a crash cannot leave half of one.

Transcripts, voiceprints, settings and the agent's records are all written
straight to their final path. Interrupt that -- power loss, a kill, a full
disk -- and the file is truncated JSON, which reads as corrupt rather than as
missing. Missing is recoverable: the pipeline notices and re-transcribes.
Corrupt is not, and it looks like data until something tries to parse it.

Writing to a temporary file in the same directory and renaming is atomic on
POSIX: the rename either happens or it does not, and readers see the old file
or the new one, never a partial one. Same directory matters -- rename across
filesystems is not atomic.
"""

import json
import os
import tempfile


def write_bytes(path, data):
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            # The rename is atomic; the contents reaching the platter are not
            # implied by it. Without the fsync a crash can leave a
            # correctly-named file full of zeroes.
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text(path, text, encoding="utf-8"):
    write_bytes(path, text.encode(encoding))


def write_json(path, obj, **kwargs):
    kwargs.setdefault("allow_nan", False)
    write_text(path, json.dumps(obj, **kwargs))


def append_line(path, text):
    """Append one line, flushed.

    Append of a single small write is atomic enough for a log that is read
    line by line: a torn final line is dropped by the reader, and nothing
    earlier is affected. Rewriting the whole file to add a line would be the
    riskier choice.
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())
