"""Deleting a recording has to delete what it said.

Found by review 2026-09-17: deletion removed the audio, the transcript, the
keyword rows and the semantic segments, and left the units alone -- which is
where the words live for `search_units`, exposed to models over MCP. A
conversation deleted on purpose stayed fully readable indefinitely, because
unit rows store the text themselves and unit search never checks whether the
clip behind them still exists.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))
sys.path.insert(0, os.path.join(HERE, "..", "host"))


def test_units_can_be_purged_for_one_clip():
    import units
    assert hasattr(units, "remove_clip")


def test_deletion_reaches_the_unit_store():
    src = open(os.path.join(HERE, "..", "web", "server.py")).read()
    body = src[src.index("def delete_clip_files"):]
    body = body[:body.index("\n@app.")]
    assert "units.remove_clip" in body, (
        "deleting a recording must purge its units, or its words stay "
        "searchable through the MCP tools")


def test_it_purges_units_that_merely_draw_on_the_clip():
    """A unit is a sentence with the clip boundary undone, so the clip being
    deleted is often not the one in its `clip` column."""
    import inspect
    import units
    src = inspect.getsource(units.remove_clip)
    assert "clips LIKE" in src or "clips like" in src, (
        "a unit spanning two clips still contains the deleted one's words")


def test_every_store_that_keeps_words_is_named_in_deletion():
    """The failure was a store nobody remembered. Name them all here so the
    next one added has somewhere obvious to be missed from."""
    src = open(os.path.join(HERE, "..", "web", "server.py")).read()
    body = src[src.index("def delete_clip_files"):]
    body = body[:body.index("\n@app.")]
    for store in ("index_db.remove_clip",      # keyword search
                  "semantic.remove_clip",      # meaning search, segments
                  "units.remove_clip"):        # meaning search, sentences
        assert store in body, f"{store} is not called on deletion"
