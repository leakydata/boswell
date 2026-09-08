"""Which recorders this install actually has.

Until now the answer was hardcoded: one panel for a board called Boswell,
one for an Omi, both always on screen. That is right for the person who
built both and wrong for everybody else -- somebody who owns an Omi and
nothing else opened the Device page and found a large panel about a
handmade board they will never have, with a Connect button that cannot
succeed.

So the recorders are a list, kept here. A panel exists because a recorder
was paired, not because the code knows the make.

Seeded rather than started empty. An install with three thousand clips in
it already knows which recorders it has -- they are stamped on the clips --
and asking that person to pair devices they have been using for a month
would be a worse first run than the one this replaces.
"""

import os
import time

import atomicio

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
PATH = os.path.join(DATA, "recorders.json")

# What a recorder can be. The kind decides which panel is drawn and which
# process does the talking: `boswell` is spoken to by this server over its
# own connection, `omi` by the omid daemon over its.
KINDS = ("boswell", "omi")


def norm_id(addr):
    """A Bluetooth address reduced to the form the archive stamps on clips."""
    if not addr:
        return None
    s = "".join(c for c in str(addr).lower() if c in "0123456789abcdef")
    return s or None


def _read():
    try:
        import json
        with open(PATH) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("recorders"), list):
        return None
    return d


def _write(rows):
    atomicio.write_json(PATH, {"recorders": rows}, indent=2)
    return rows


def load(seed=None):
    """The paired recorders. Seeds itself once if the file is not there yet.

    `seed` is called only when there is no file, and returns the rows to
    start from -- which for an existing archive is whatever has been
    recording into it.
    """
    d = _read()
    if d is not None:
        return d["recorders"]
    rows = list(seed() if seed else [])
    _write(rows)
    return rows


def add(kind, address, name=None):
    """Pair one. Adding a recorder already paired updates it rather than
    listing it twice -- re-pairing after a board swap is the ordinary case,
    and two panels for one device would be worse than a stale name."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    ident = norm_id(address)
    if not ident:
        raise ValueError("a recorder needs a Bluetooth address")
    rows = [r for r in load() if r.get("id") != ident]
    rows.append({"id": ident, "kind": kind, "address": address,
                 "name": name or None, "added": time.time()})
    rows.sort(key=lambda r: r.get("added") or 0)
    _write(rows)
    return rows


def remove(ident):
    ident = norm_id(ident) or ident
    rows = load()
    kept = [r for r in rows if r.get("id") != ident]
    if len(kept) != len(rows):
        _write(kept)
    return kept


def of_kind(kind):
    return [r for r in load() if r.get("kind") == kind]


def first_of(kind):
    """The recorder of this kind to actually talk to.

    Oldest first was the whole rule, which is fine with one of a kind and
    wrong the moment there are two: a second Omi-protocol device could be
    paired, listed and never once used, because the first one paired always
    won and the only way to reach the other was to forget the first.

    Boswell records from one device of a kind at a time -- the radio is
    exclusive and a second stream is a second connection competing for it --
    so which one is a choice somebody has to be able to make. `primary` is
    that choice; without it, oldest still wins.
    """
    rows = of_kind(kind)
    if not rows:
        return None
    for r in rows:
        if r.get("primary"):
            return r
    return rows[0]


def set_primary(ident):
    """Choose which recorder of its kind is the one to record from.

    Exclusive within the kind and not across it: choosing an Omi says
    nothing about which handmade board to use, and clearing every other
    kind's choice would be a surprise nobody asked for.
    """
    ident = norm_id(ident) or ident
    rows = load()
    me = next((r for r in rows if r.get("id") == ident), None)
    if not me:
        raise ValueError(f"no recorder {ident!r}")
    for r in rows:
        if r.get("kind") == me.get("kind"):
            r.pop("primary", None)
    me["primary"] = True
    _write(rows)
    return rows


def has(kind):
    return bool(of_kind(kind))
