#!/usr/bin/env python3
"""Attribute old clips to the recorder that made them.

    uv run host/backfill_device.py --device d966cfbb58a4          # dry run
    uv run host/backfill_device.py --device d966cfbb58a4 --write

Clips written before times records carried a device id have none, and
de-duplication reads a missing id as "could be any recorder". That was the
right default -- it is what stopped the first import after device ids existed
from duplicating the entire archive -- but it is a weaker guarantee than the
truth, and the truth is knowable here: only one recorder existed then.

What is written is `device_id`, plus `device_id_inferred: true`. The second
field is the point. A recorder that stamped its own name into a file is not
the same kind of fact as a person concluding afterwards which device it must
have been, and an archive that cannot tell those apart has quietly lost the
ability to say how it knows anything.

It matters here specifically: the board this project runs on was replaced
partway through, so some of these clips came from a different physical device
with a different Bluetooth address. They are the same recorder in every sense
that matters -- one project, one lanyard, never two at once -- and they are
not the same hardware. Inferred says exactly that much and no more.
"""

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))

import atomicio
import clipwriter


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", required=True,
                    help="the recorder id to attribute them to")
    ap.add_argument("--write", action="store_true",
                    help="actually change the records")
    ap.add_argument("--before", type=float,
                    help="only clips started before this epoch")
    args = ap.parse_args()

    times = clipwriter.TIMES
    if not os.path.isdir(times):
        print("no times records to work on")
        return 1

    todo, skipped = [], 0
    for name in sorted(os.listdir(times)):
        if not name.endswith(".json"):
            continue
        rec = load(os.path.join(times, name))
        if rec is None:
            continue
        if rec.get("device_id"):
            skipped += 1
            continue
        if args.before and (rec.get("started") or 0) >= args.before:
            continue
        todo.append((name, rec))

    print(f"{len(todo)} clip(s) without a recorder, {skipped} already named")
    if not todo:
        return 0

    span = [r.get("started") or 0 for _, r in todo if r.get("started")]
    if span:
        print(f"  from {time.strftime('%Y-%m-%d', time.localtime(min(span)))}"
              f" to {time.strftime('%Y-%m-%d', time.localtime(max(span)))}")

    if not args.write:
        print("\ndry run -- pass --write to make the change")
        for name, _ in todo[:5]:
            print(f"  would set device_id on {name}")
        if len(todo) > 5:
            print(f"  ... and {len(todo) - 5} more")
        return 0

    # A copy first. Three thousand small rewrites is exactly the shape of
    # operation that is fine until it is not, and the originals are the only
    # record of when this audio happened.
    backup = os.path.join(clipwriter.DATA, "backups",
                          f"times-{int(time.time())}")
    os.makedirs(os.path.dirname(backup), exist_ok=True)
    shutil.copytree(times, backup)
    print(f"copied the originals to {backup}")

    done = failed = 0
    for name, rec in todo:
        rec["device_id"] = args.device
        # Not the same kind of fact as a device stamping its own name.
        rec["device_id_inferred"] = True
        try:
            atomicio.write_json(os.path.join(times, name), rec)
            done += 1
        except Exception as e:
            print(f"  {name}: {e}")
            failed += 1

    print(f"{done} attributed to {args.device}, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
