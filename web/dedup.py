"""Has this audio already been ingested?

Once the device records to its card as well as streaming live, the same
audio reaches the host twice: once over the air while it was happening, and
again in the catch-up transfer afterwards. Ingesting both produces a
conversation that occurred twice, which reads as a real event rather than as
an error -- so this has to be right before the second path exists, not after.

The key is (device_id, boot_id, device_ms), not wall-clock time.

Device milliseconds are an uptime counter: monotonic within a boot, and
unrelated to anything outside it. That is precisely why they are the right
key. Wall-clock times on the host drift, are assigned by whichever path
ingested the audio, and differ by however long the catch-up took -- two
records of the same thirty seconds can easily disagree by hours. The uptime
span does not disagree with itself, because both copies were stamped by the
same counter on the same device at the moment of capture.

What the boot id adds is the part that makes uptime usable at all: 41,900 ms
happens once per boot, and without knowing which boot, two unrelated clips
from different sessions look like the same audio.

And what the device id adds is the same argument one level up. A boot id is
sixteen random bits, so two recorders in one house collide on one sooner than
feels possible -- and when they do, the audio quietly merged is from two
different rooms. Which is a failure with no symptom: no error, no duplicate,
just a conversation that never happened.
"""

# Clips are written on a frame boundary, and the two paths need not agree
# about which frame ended a clip, so spans that describe the same audio can
# differ slightly at the edges. A frame is 20 ms; a couple of frames of slack
# stops a boundary difference reading as new audio.
EDGE_SLACK_MS = 60


def overlap_ms(a, b):
    """Milliseconds two [start, end] spans have in common."""
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def covered_fraction(span, held):
    """How much of `span` the already-held spans account for, 0.0 to 1.0.

    A fraction rather than a yes or no, because partial overlap is the
    normal case and the two ends of it want opposite handling: a span the
    archive already covers almost entirely is a duplicate, and one it barely
    touches is new audio that happens to abut something old.

    `held` need not be sorted and may overlap itself; the same millisecond
    counted twice would report more coverage than exists and hide real audio,
    so overlapping held spans are merged before measuring.
    """
    start, end = span
    length = end - start
    if length <= 0:
        return 1.0          # nothing to ingest is trivially already held
    total = 0
    for lo, hi in _merged(held):
        total += overlap_ms((start, end), (lo, hi))
    return min(1.0, total / float(length))


def _merged(spans):
    out = []
    for lo, hi in sorted(tuple(s) for s in spans):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def same_device(a, b):
    """Could these two records have come from the same recorder?

    Unknown matches anything, deliberately. Every clip in the archive before
    this existed carries no device id, and they are all from the one device
    that has ever fed it -- so treating "unknown" as "not a match" would make
    the next import of an already-held recording a duplicate of every one of
    them. Once two recorders are actually in use, both sides carry an id and
    are told apart properly.

    This is the opposite of how a missing boot id is treated, and the
    asymmetry is the point. A missing boot id makes a span unplaceable, so
    keeping the audio is the safe answer. A missing device id only makes it
    unattributed, and the span still says everything needed.
    """
    return a is None or b is None or a == b


def is_duplicate(boot_id, span, records, threshold=0.9, device_id=None):
    """Is this span already in the archive?

    `records` are times records as written beside each clip. Only those from
    the same boot on the same device are considered: a different boot is a
    different counter, and a different device is a different counter again --
    two recorders in one house will collide on a sixteen-bit boot id sooner
    than feels possible, and the audio they merge is from two different rooms.

    A record with no boot id cannot be placed. Those are clips written before
    the id was recorded, and they are treated as *not* matching -- ingesting
    the same audio twice is a visible, fixable mess, while discarding audio
    on a guess is silent and permanent. When in doubt, keep it.
    """
    if boot_id is None:
        return False
    held = [r["device_ms"] for r in records
            if r.get("boot_id") == boot_id and r.get("device_ms")
            and None not in r["device_ms"]
            and same_device(device_id, r.get("device_id"))]
    if not held:
        return False
    start, end = span
    return covered_fraction((start - EDGE_SLACK_MS, end + EDGE_SLACK_MS),
                            held) >= threshold


def new_spans(boot_id, span, records, device_id=None):
    """The parts of `span` the archive does not already hold.

    For a transfer that overlaps the live stream only at one end, which is
    the ordinary case: the host was listening, went away mid-conversation,
    and the card carries the whole thing. Returning the gap rather than the
    whole span means the overlap is not ingested a second time and the part
    nobody heard is not thrown away with it.
    """
    start, end = span
    if end <= start:
        return []
    held = _merged([r["device_ms"] for r in records
                    if boot_id is not None and r.get("boot_id") == boot_id
                    and r.get("device_ms") and None not in r["device_ms"]
                    and same_device(device_id, r.get("device_id"))])
    gaps, cursor = [], start
    for lo, hi in held:
        if hi <= cursor or lo >= end:
            continue
        if lo > cursor:
            gaps.append((cursor, min(lo, end)))
        cursor = max(cursor, hi)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(a, b) for a, b in gaps if b - a > EDGE_SLACK_MS]
