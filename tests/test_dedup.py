"""De-duplicating audio that arrives twice.

Live over the air and again in the catch-up transfer. Getting this wrong in
one direction duplicates a conversation, which reads as a real event; in the
other it discards audio nobody heard. Both are silent, so these are
deliberately unkind.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))

import dedup


def rec(boot, lo, hi, name="clip.wav"):
    return {"name": name, "boot_id": boot, "device_ms": [lo, hi]}


# ------------------------------------------------------------ the basics

def test_the_same_span_twice_is_a_duplicate():
    held = [rec(7, 10000, 40000)]
    assert dedup.is_duplicate(7, (10000, 40000), held)


def test_untouched_audio_is_not_a_duplicate():
    held = [rec(7, 10000, 40000)]
    assert not dedup.is_duplicate(7, (60000, 90000), held)


def test_an_empty_archive_duplicates_nothing():
    assert not dedup.is_duplicate(7, (0, 30000), [])


# ------------------------------------------------- the reboot, which is the
#                                                    whole reason for boot_id

def test_the_same_uptime_after_a_reboot_is_different_audio():
    """41,900 ms happens once per boot. Without the id these are the same
    span, and the second session's audio would be silently discarded."""
    held = [rec(7, 10000, 40000)]
    assert not dedup.is_duplicate(8, (10000, 40000), held)


def test_a_reboot_mid_transfer_does_not_match_across_the_boundary():
    held = [rec(7, 0, 30000), rec(8, 0, 30000)]
    assert dedup.is_duplicate(7, (0, 30000), held)
    assert dedup.is_duplicate(8, (0, 30000), held)
    assert not dedup.is_duplicate(9, (0, 30000), held)


def test_a_clip_with_no_boot_id_never_matches():
    """Clips written before the id was recorded. Ingesting twice is visible
    and fixable; discarding on a guess is silent and permanent."""
    held = [{"name": "old.wav", "device_ms": [10000, 40000]}]
    assert not dedup.is_duplicate(7, (10000, 40000), held)
    assert not dedup.is_duplicate(None, (10000, 40000), [rec(7, 10000, 40000)])


# ------------------------------------------------------- partial overlap

def test_a_boundary_frame_difference_is_still_a_duplicate():
    """The two paths need not agree about which frame ended a clip."""
    held = [rec(7, 10000, 40000)]
    assert dedup.is_duplicate(7, (10020, 39980), held)
    assert dedup.is_duplicate(7, (9980, 40020), held)


def test_half_covered_is_not_a_duplicate():
    held = [rec(7, 10000, 25000)]
    assert not dedup.is_duplicate(7, (10000, 40000), held)


def test_adjacent_held_spans_together_cover_one_long_one():
    held = [rec(7, 0, 15000), rec(7, 15000, 30000)]
    assert dedup.is_duplicate(7, (0, 30000), held)


def test_overlapping_held_spans_are_not_double_counted():
    """Counting the same millisecond twice would report more coverage than
    exists, and hide real audio behind a phantom."""
    held = [rec(7, 0, 20000), rec(7, 0, 20000), rec(7, 0, 20000)]
    assert not dedup.is_duplicate(7, (0, 40000), held)
    assert dedup.covered_fraction((0, 40000), [(0, 20000)] * 3) == pytest.approx(0.5)


# ----------------------------------------------------------- the gap case

def test_only_the_unheard_part_comes_back():
    """The host listened to the first half, went away, and the card has the
    lot. Ingest the remainder, not the overlap and not nothing."""
    held = [rec(7, 0, 30000)]
    gaps = dedup.new_spans(7, (0, 90000), held)
    assert gaps == [(30000, 90000)]


def test_a_hole_in_the_middle_comes_back():
    held = [rec(7, 0, 30000), rec(7, 60000, 90000)]
    assert dedup.new_spans(7, (0, 90000), held) == [(30000, 60000)]


def test_nothing_held_returns_the_whole_span():
    assert dedup.new_spans(7, (0, 90000), []) == [(0, 90000)]
    assert dedup.new_spans(None, (0, 90000), [rec(7, 0, 30000)]) == [(0, 90000)]


def test_a_fully_held_span_returns_no_gaps():
    assert dedup.new_spans(7, (0, 30000), [rec(7, 0, 30000)]) == []


def test_slivers_are_not_worth_ingesting():
    """A gap shorter than the slack is a boundary artefact, not audio."""
    held = [rec(7, 0, 30000), rec(7, 30040, 60000)]
    assert dedup.new_spans(7, (0, 60000), held) == []


# ------------------------------------------------------------- edge cases

def test_a_zero_length_span_is_already_held():
    assert dedup.covered_fraction((5000, 5000), []) == 1.0
    assert dedup.new_spans(7, (5000, 5000), []) == []


def test_held_spans_need_not_be_sorted():
    held = [rec(7, 60000, 90000), rec(7, 0, 30000), rec(7, 30000, 60000)]
    assert dedup.is_duplicate(7, (0, 90000), held)
    assert dedup.new_spans(7, (0, 90000), held) == []


def test_a_record_missing_its_span_is_ignored_not_fatal():
    held = [{"name": "x", "boot_id": 7}, {"name": "y", "boot_id": 7,
                                          "device_ms": [None, None]},
            rec(7, 0, 30000)]
    assert dedup.is_duplicate(7, (0, 30000), held)
    assert dedup.new_spans(7, (0, 60000), held) == [(30000, 60000)]
