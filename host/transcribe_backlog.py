#!/usr/bin/env python3
"""Work through the transcription backlog on its own, without the server.

    uv run host/transcribe_backlog.py                 # everything outstanding
    uv run host/transcribe_backlog.py --limit 200     # a bite of it
    uv run host/transcribe_backlog.py --dry-run       # what is outstanding
    uv run host/transcribe_backlog.py --newest-first  # today before last month

Transcription, diarization, voiceprints and sound tagging all happen here,
exactly as they do inside `web/server.py` -- this calls the same
`pipeline.Worker._process()`, so a clip finished by this script is
indistinguishable from one finished by the server, and the archive, the search
indexes and the speaker store are all updated the same way.

**Why this exists.** The only way to transcribe anything used to be to run the
whole server: a web interface, a websocket, a capture supervisor, a reviewing
agent and a background sweep, all to do a job that needs none of them. That
matters on this machine because the server is also the biggest GPU tenant --
about 7.5 GB resident -- and it is routinely stopped so the card can be used
for something else. Wanting the backlog cleared should not mean starting a web
server, and clearing it should not mean leaving one running.

**The one rule: do not run this while the server is running.** Both would be
writing the same transcripts, the same index rows and the same voiceprints,
and the losing write disappears silently. This refuses to start if the server
is up, and `--force` is deliberately not offered.

Progress and failures both go to stdout. That is not decoration: transcription
failures inside the server were reported only to a websocket, so 5,991 clips
failed over seven hours with nothing in the journal and no visible symptom but
a count that had not moved.
"""

import argparse
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "web"))


def _load_env_file():
    """Read .env into the environment, the way the server does at startup.

    Diarization needs HF_TOKEN and degrades **silently** without it: every
    segment comes back with speaker None and the transcript looks fine with
    nobody in it. A standalone tool that skipped this would quietly produce a
    whole backlog of speaker-less transcripts, which is worse than failing.
    """
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return False
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(),
                              value.strip().strip('"').strip("'"))
    return True


def _server_is_running():
    """Is web/server.py up? Two writers would corrupt each other quietly.

    Asked two ways, because they disagree for minutes at a time. The socket
    is the obvious check and it is not sufficient: the unit spends up to
    `TimeoutStartSec=300` loading models before it binds anything, and during
    that window systemd reports it **active** while nothing answers on the
    port. A guard that only dialled the socket therefore waved the standalone
    run straight through into exactly the two-writer collision it exists to
    prevent -- observed, not theorised, on the first run of this script.

    Returns a description of what is up, or None.
    """
    import socket
    import subprocess

    try:
        import netcfg
        port = netcfg.port()
    except Exception:
        port = 8740

    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect(("127.0.0.1", port))
        return f"answering on port {port}"
    except OSError:
        pass
    finally:
        s.close()

    # Starting, stopping, or loading models -- active but not yet listening.
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "boswell.service"],
            capture_output=True, text=True, timeout=10)
        state = (r.stdout or "").strip()
        if state in ("active", "activating", "reloading", "deactivating"):
            return f"boswell.service is {state} (not yet listening)"
    except Exception:
        pass                     # no systemd, or not this user's -- carry on
    return None


def outstanding(newest_first=False):
    """Clips with no transcript yet, oldest first unless asked otherwise."""
    import pipeline
    names = [f for f in os.listdir(pipeline.DATA) if f.endswith(".wav")]
    todo = [f for f in names
            if not os.path.exists(pipeline.transcript_path(f))]
    todo.sort(reverse=newest_first)
    return todo


def main():
    ap = argparse.ArgumentParser(
        description="Transcribe and diarize whatever the archive is missing.")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many clips")
    ap.add_argument("--newest-first", action="store_true",
                    help="today's recordings before last month's")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what is outstanding and stop")
    args = ap.parse_args()

    had_env = _load_env_file()

    import pipeline
    todo = outstanding(args.newest_first)
    if not todo:
        print("nothing outstanding")
        return 0

    total = len(todo)
    secs = total * pipeline.CLIP_SECONDS if hasattr(pipeline, "CLIP_SECONDS") \
        else total * 30
    print(f"{total:,} clip(s) outstanding, about {secs/3600:.1f} h of audio")
    if args.dry_run:
        for f in todo[:10]:
            print(f"  {f}")
        if total > 10:
            print(f"  ... and {total - 10:,} more")
        return 0

    up = _server_is_running()
    if up:
        print(f"\nThe server is up -- {up}. Stop it first:\n"
              f"    systemctl --user stop boswell.service\n\n"
              f"Both of us would be writing the same transcripts, index rows "
              f"and voiceprints, and the losing write vanishes without a "
              f"word.", file=sys.stderr)
        return 2

    if not had_env:
        print("warning: no .env found -- without HF_TOKEN diarization is "
              "skipped silently and every transcript comes out with nobody "
              "in it", file=sys.stderr)

    if args.limit:
        todo = todo[:args.limit]

    print("loading models (about 20s)...", flush=True)
    worker = pipeline.Worker.__new__(pipeline.Worker)   # no queue, no threads
    worker.notify = lambda *a, **k: None
    worker.on_transcript = lambda *a, **k: None
    worker._asr = worker._align = worker._diar = worker._sound = None
    import threading
    worker._loadlock = threading.Lock()
    worker._gpu = threading.Lock()
    worker._qlock = threading.Lock()
    worker._busy = set()
    worker._queued = set()
    worker._transcriber = "local"
    worker._load()
    worker._load_helpers()
    if worker._diar is None:
        print("warning: no diarizer -- transcripts will have no speakers",
              file=sys.stderr)

    done = failed = 0
    began = time.time()
    try:
        for i, clip in enumerate(todo, 1):
            t0 = time.time()
            try:
                worker._process(clip)
                done += 1
                el = time.time() - began
                rate = done / el * 60 if el else 0
                left = (len(todo) - i) / rate if rate else 0
                print(f"  [{i}/{len(todo)}] {clip}  {time.time()-t0:.1f}s"
                      f"   {rate:.1f}/min   ~{left/60:.1f}h left", flush=True)
            except Exception as e:
                failed += 1
                # Loudly, and to stdout. A failure nobody can see in the log
                # is a failure nobody finds.
                print(f"  [{i}/{len(todo)}] FAILED {clip}: "
                      f"{type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
    except KeyboardInterrupt:
        # Safe anywhere: each clip's transcript is written whole, and the next
        # run picks up whatever is still missing.
        print("\nstopped; run again to continue where this left off")

    el = time.time() - began
    print(f"\n{done:,} transcribed, {failed:,} failed, in {el/60:.1f} min"
          + (f"  ({done/el*60:.1f}/min)" if el else ""))
    return 1 if failed and not done else 0


if __name__ == "__main__":
    sys.exit(main())
