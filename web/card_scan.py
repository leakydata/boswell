"""Find a Boswell card that has been docked.

The device appears as an ordinary USB drive, and whatever the desktop does
with it -- automount under /media, /run/media, or nothing at all -- is not
something the server gets told about. So it looks.

Recognition is by content, not by label or device node. A card formatted by
this firmware has a `boswell/` directory with `.bwl` files in it, and that is
both necessary and sufficient: it identifies a card that was written by a
Boswell wherever it has been mounted, including a directory somebody copied
the files into by hand, and it will not claim a random USB stick that happens
to be the right size.
"""

import getpass
import os

CARD_DIR = "boswell"
SUFFIX = ".bwl"

# Where desktops put removable media, plus the places a person mounts things
# by hand. Cheap to check and there are not many.
def _roots():
    user = getpass.getuser()
    out = ["/media", "/mnt", "/run/media", f"/media/{user}", f"/run/media/{user}",
           os.path.expanduser("~/mnt")]
    seen, uniq = set(), []
    for r in out:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def looks_like_card(path):
    """Is this directory a docked card, or the card's own folder?"""
    for candidate in (os.path.join(path, CARD_DIR), path):
        try:
            if not os.path.isdir(candidate):
                continue
            for name in os.listdir(candidate):
                if name.endswith(SUFFIX):
                    return candidate
        except OSError:
            continue
    return None


def recordings(path):
    """The .bwl files in a card directory, oldest name first."""
    try:
        return sorted(os.path.join(path, n) for n in os.listdir(path)
                      if n.endswith(SUFFIX))
    except OSError:
        return []


def find_cards(roots=None):
    """Every docked card found, as the directory holding the recordings.

    Deliberately shallow. Walking a mounted filesystem to find a directory is
    slow on a 16 GB card and worse on whatever else the machine has mounted,
    and the answer is always one or two levels down.
    """
    found = []
    for root in (roots if roots is not None else _roots()):
        if not os.path.isdir(root):
            continue

        hit = looks_like_card(root)
        if hit:
            found.append(hit)
            continue

        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for name in entries:
            hit = looks_like_card(os.path.join(root, name))
            if hit:
                found.append(hit)

    seen, uniq = set(), []
    for f in found:
        real = os.path.realpath(f)
        if real not in seen:
            seen.add(real)
            uniq.append(f)
    return uniq
