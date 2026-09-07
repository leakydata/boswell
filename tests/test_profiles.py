"""A profile used to be role and note and nothing else.

The owner wants to say more about a person: how to say their name, and the
other names they go by. Both are things a person types once and reads often,
so the rules that matter are the quiet ones -- an alias list that silently
grows, a "Bob" and a "bob" sitting next to each other, a database from last
month that gains the new columns only if the migration actually fires.

These tests pin those pieces: the migration on a database that predates the
columns, the set/get round trip, the cleaning rules for aliases, the API
turning a wrong type into a 400 that names the field, and /api/speakers
handing the new fields to the interface.
"""
import asyncio
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))
import speaker_store as store            # noqa: E402
import pipeline                          # noqa: E402
import server                            # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A store of its own, and a clean cache to go with it."""
    monkeypatch.setattr(store, "DB", str(tmp_path / "speakers.db"))
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    store._cache.update(version=None, ids=None, pids=None, M=None)
    store._bump()
    return store


@pytest.fixture
def ada(db):
    """One named person, already carrying the old profile fields."""
    pid = db.person_id_for("Ada")
    db.set_profile(pid, role="friend", note="met at the lake")
    return pid


# ------------------------------------------------------------- the migration

def test_an_old_database_gains_the_columns_and_keeps_what_it_had(db, tmp_path):
    # A database written before pronunciation and aliases existed must come
    # up working, with everything it already held still there.
    old = tmp_path / "speakers.db"
    c = sqlite3.connect(old)
    c.executescript("""
        CREATE TABLE people (
            id      INTEGER PRIMARY KEY,
            name    TEXT UNIQUE,
            kind    TEXT,
            created REAL,
            role    TEXT,
            note    TEXT
        );
        INSERT INTO people (id, name, kind, created, role, note)
        VALUES (1, 'Ada', 'person', 0.0, 'friend', 'met at the lake');
    """)
    c.commit()
    c.close()

    db.profile(1)                       # opens through _conn, which migrates

    cols = {r[1] for r in sqlite3.connect(old).execute(
        "PRAGMA table_info(people)")}
    assert {"pronunciation", "aliases"} <= cols
    p = db.profile(1)
    assert p["role"] == "friend" and p["note"] == "met at the lake"
    assert p["pronunciation"] is None
    assert p["aliases"] == []


# ------------------------------------------------------------- set and get

def test_a_profile_round_trips_through_set_and_get(db, ada):
    db.set_profile(ada, pronunciation="AY-da", aliases=["Adie", "Adz"])
    p = db.profile(ada)
    assert p["pronunciation"] == "AY-da"
    assert p["aliases"] == ["Adie", "Adz"]
    assert isinstance(p["aliases"], list), "the JSON must never leak through"


def test_the_everyone_listing_carries_the_new_fields_too(db, ada):
    db.set_profile(ada, pronunciation="AY-da", aliases=["Adie"])
    row = next(r for r in db.people() if r["id"] == ada)
    assert row["pronunciation"] == "AY-da"
    assert row["aliases"] == ["Adie"]


def test_an_empty_alias_list_clears_the_field(db, ada):
    db.set_profile(ada, aliases=["Adie"])
    db.set_profile(ada, aliases=[])
    assert db.profile(ada)["aliases"] == []


def test_a_field_not_sent_stays_alone_and_an_empty_string_clears_it(db, ada):
    db.set_profile(ada, pronunciation="AY-da")
    db.set_profile(ada, note="moved away")          # pronunciation untouched
    assert db.profile(ada)["pronunciation"] == "AY-da"
    db.set_profile(ada, note="")
    assert db.profile(ada)["note"] is None


# ---------------------------------------------------------- alias cleaning

def test_aliases_are_stripped_empties_dropped_and_deduped(db, ada):
    db.set_profile(ada, aliases=["  Adie  ", "", "adie", "  ", "Adz"])
    assert db.profile(ada)["aliases"] == ["Adie", "Adz"]


def test_nine_aliases_keep_the_first_eight(db, ada):
    db.set_profile(ada, aliases=[f"name {i}" for i in range(9)])
    assert db.profile(ada)["aliases"] == [f"name {i}" for i in range(8)]


def test_a_long_alias_is_cut_to_forty_characters(db, ada):
    db.set_profile(ada, aliases=["x" * 55])
    assert db.profile(ada)["aliases"] == ["x" * 40]


def test_something_that_is_not_a_list_of_strings_is_refused(db, ada):
    for bad in ("Bob", 3, ["Bob", 3], [None]):
        with pytest.raises(ValueError):
            db.set_profile(ada, aliases=bad)


def test_a_non_string_pronunciation_is_refused(db, ada):
    with pytest.raises(ValueError):
        db.set_profile(ada, pronunciation=7)


# ------------------------------------------------------------------ the API

def post(pid, body):
    return asyncio.run(server.api_voice_profile(pid, body))


class TestProfileApi:
    """The endpoint is what the interface actually talks to, so the wrong
    type must come back as a 400 that names the field, and the speakers list
    must carry what the profile form just saved."""

    def test_the_endpoint_takes_the_new_fields(self, db, ada):
        out = post(ada, {"role": "friend", "pronunciation": "AY-da",
                         "aliases": ["Adie", "adie", "  Adz "]})
        assert out["pronunciation"] == "AY-da"
        assert out["aliases"] == ["Adie", "Adz"]

    def test_the_endpoint_rejects_wrong_types_with_a_400(self, ada):
        from fastapi import HTTPException
        for bad in ({"pronunciation": 7}, {"role": []},
                    {"aliases": "Bob"}, {"aliases": [1]}, {"aliases": {}}):
            with pytest.raises(HTTPException) as e:
                post(ada, bad)
            assert e.value.status_code == 400, bad

    def test_the_speakers_list_exposes_the_new_fields(self, db, ada,
                                                      monkeypatch):
        monkeypatch.setattr(pipeline, "_migrated", True)
        db.set_profile(ada, note="met at the lake", pronunciation="AY-da",
                       aliases=["Adie"])
        rows = asyncio.run(server.api_speakers())
        row = next(r for r in rows if r["person_id"] == ada)
        assert row["note"] == "met at the lake"
        assert row["pronunciation"] == "AY-da"
        assert row["aliases"] == ["Adie"]
