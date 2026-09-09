"""Searching things people said, rather than transcript lines.

A line is whatever fell inside a thirty-second clip: median seven words across
this archive, 61% of them under ten. "but it has a subscription" carries no
meaning to embed. Stitched units are the same speech with the boundary undone.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))

import units


def read_file(p):
    with open(os.path.join(HERE, "..", p)) as f:
        return f.read()


def test_the_index_is_rebuilt_whole_not_patched():
    """Units are derived -- they move when a transcript is edited, a name is
    applied or a clip is re-transcribed. Keeping derived data correct by
    invalidation is what produced this project's worst bugs: a storage figure
    twenty-one hours stale beside a battery reading a minute old. A full
    rebuild takes under two minutes, so there is nothing to invalidate.
    """
    src = read_file("web/units.py")
    fn = src[src.index("def rebuild("):]
    fn = fn[:fn.index("\ndef ")]
    assert "DELETE FROM unit" in fn, "it patches rather than replacing"
    assert "on_progress" in fn, "a two-minute job with no progress is a hang"


def test_the_swap_is_one_transaction_not_a_rename():
    """Renaming looked cleaner and does not work: a sqlite-vec vec0 table owns
    shadow tables, and ALTER TABLE RENAME moves the virtual table without
    them, leaving an index that fails on the first search with
    `no such table: main.unit_vec_chunks`.
    """
    src = read_file("web/units.py")
    fn = src[src.index("def rebuild("):]
    fn = fn[:fn.index("\ndef ")]
    assert 'db.execute("BEGIN")' in fn, "the replacement is not atomic"
    assert "ALTER TABLE" not in fn, "renaming a vec0 table orphans its shadows"
    # And WAL, so readers keep the old index until the commit lands.
    assert "journal_mode=WAL" in src


def test_a_unit_carries_what_a_filter_needs():
    # The point of metadata beside the vector: "what did Blase say about the
    # roof, while I was at the desk, last week" is three kinds of question
    # and only one of them is semantic.
    src = read_file("web/units.py")
    schema = src[src.index("def _schema("):]
    schema = schema[:schema.index("\ndef ")]
    for field in ("at", "until", "time_known", "speaker", "name",
                  "device_id", "clips", "sounds", "section", "conv"):
        assert field in schema, f"a unit cannot be filtered by {field}"


def test_the_untrusted_sound_tags_are_excluded():
    """`Speech` sits on 93% of clips and separates nothing. `Vehicle` is
    worse: 450 clips carry it, 364 between midnight and 5am and 248 of those
    silent -- a microphone beside a fan. The tagger picks it *instead of*
    `Mechanical fan`, and reaches for `Train` and `Boat` too.
    """
    assert "Speech" in units.UNTRUSTED_SOUNDS
    assert "Vehicle" in units.UNTRUSTED_SOUNDS
    for neighbour in ("Train", "Boat, Water vehicle", "Rail transport"):
        assert neighbour in units.UNTRUSTED_SOUNDS, \
            f"{neighbour} is the same rumble misheard"
    # Tags that passed the hour-of-day and silence test stay searchable.
    for good in ("Typing", "Computer keyboard", "Dog", "Bark", "Music"):
        assert good not in units.UNTRUSTED_SOUNDS


def test_sounds_span_the_clips_a_unit_draws_on():
    # Tags are per clip and a unit can cross a boundary; the situation
    # genuinely spans it, so the union is right.
    src = read_file("web/units.py")
    fn = src[src.index("def _sounds_for("):]
    fn = fn[:fn.index("\ndef ")]
    assert "for name in clips" in fn
    assert "sounds_strong" in fn, "the low-bar column excludes silently"


def test_the_line_index_is_kept_not_replaced():
    """They answer different questions: `seg` for exact quotes, precise
    jump-to-audio and keyword matching; `unit` for what something was about.
    """
    src = read_file("web/units.py")
    assert "CREATE TABLE IF NOT EXISTS {table}" in src
    # Nothing here drops or rewrites the line index.
    assert "DROP TABLE" not in src.replace("DROP TABLE IF EXISTS unit_old", "")
    assert "seg" not in src[src.index("def rebuild("):src.index("def search(")] \
        .replace("segment", "").replace("semantic", "")


def test_a_search_can_be_narrowed_without_a_second_query():
    src = read_file("web/units.py")
    sig = src[src.index("def search("):src.index("def search(") + 200]
    for f in ("device", "name", "sound", "since", "until"):
        assert f in sig, f"cannot narrow a search by {f}"


def test_filtering_over_fetches_so_a_page_is_not_emptied():
    # Filtering after the vector search can empty a page that had matches
    # further down the list.
    src = read_file("web/units.py")
    fn = src[src.index("def search("):]
    fn = fn[:fn.index("\ndef ")]
    assert "limit * 8" in fn or "limit*8" in fn


def test_an_embedder_that_is_down_is_reported_not_hidden():
    src = read_file("web/units.py")
    fn = src[src.index("def search("):]
    fn = fn[:fn.index("\ndef ")]
    assert "embedder unavailable" in fn, "a silent empty result reads as no matches"
