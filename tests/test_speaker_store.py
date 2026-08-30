"""The speaker store decides who somebody is, and holds the only data here
that cannot be regenerated.

Audio can be re-transcribed, transcripts re-diarized, voiceprints recomputed.
The labelling is hours of a person listening and deciding, and nothing rebuilds
it. It had no tests.

These cover the rules that are easy to get wrong and silent when they are --
the ones this file exists to stop from regressing rather than the ones a type
checker would catch:

  * the margin is between PEOPLE, not between rows. A well-covered person owns
    the top several references, so a raw best-minus-runner-up margin collapses
    toward zero exactly where coverage is best and rejects the matches it was
    written to protect.
  * media never confers a name, however well it scores.
  * a slot the diarizer could not keep straight is refused for automatic
    enrolment, because a blend of two voices is indistinguishable from a real
    voiceprint afterwards.
  * the cached reference matrix is invalidated by every write. A stale matrix
    means naming somebody has no effect on the next match, which looks like
    nothing happening rather than like a bug.
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "web"))
import speaker_store as store            # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A store of its own, and a clean cache to go with it."""
    monkeypatch.setattr(store, "DB", str(tmp_path / "speakers.db"))
    monkeypatch.setattr(store, "DATA", str(tmp_path))
    store._cache.update(version=None, ids=None, pids=None, M=None)
    store._bump()
    return store


def vec(seed, dim=256):
    """A deterministic unit vector. Nearby seeds are near-identical."""
    rng = np.random.default_rng(seed)
    return store.unit(rng.normal(size=dim))


def near(v, similarity=0.90, seed=0):
    """A vector at a chosen cosine distance from v, for the same voice again.

    Built to hit the similarity exactly rather than by adding noise and hoping.
    In 256 dimensions a noise term scaled by eye swamps the signal -- 0.05 of a
    standard normal has a norm of about 0.8 against a unit vector -- so "a
    little noise" silently produces two unrelated voices and the test measures
    nothing it claims to.
    """
    rng = np.random.default_rng(seed + 9999)
    n = rng.normal(size=len(v))
    n = store.unit(n - (n @ v) * v)          # orthogonal component only
    return store.unit(similarity * v + np.sqrt(1 - similarity ** 2) * n)


def test_margin_is_between_people_not_rows(db):
    """Several references for one person must not defeat the margin rule.

    The store deliberately accumulates a reference per condition, so the top
    two scores usually belong to the same person. Comparing them would drive
    the margin to nearly zero for exactly the people who are best covered.
    """
    alice = db.person_id_for("Alice")
    bob = db.person_id_for("Bob")
    base = vec(1)
    for i in range(4):                      # Alice, well covered
        db.add_voiceprint(alice, near(base, 0.88, seed=i))
    db.add_voiceprint(bob, vec(2))          # Bob, unrelated

    r = db.match(near(base, 0.88, seed=99))
    assert r["name"] == "Alice"
    assert r["decision"] == "matched"
    # The runner-up must be Bob, not Alice's second-best row.
    assert r["candidates"][1]["name"] == "Bob"
    assert r["margin"] > db.MARGIN_MIN


def test_media_never_names_anything(db):
    """A voice off a screen stays a candidate and never confers a name."""
    pid = db.person_id_for("NileRed")
    v = vec(3)
    db.add_voiceprint(pid, v)
    db.person_id_for("Someone Else")        # so a margin exists

    assert db.match(near(v, 0.95))["decision"] == "matched"
    db.set_kind(pid, db.KIND_MEDIA)
    after = db.match(near(v, 0.95))
    assert after["decision"] == "uncertain", "media must be capped at uncertain"
    assert after["name"] is None
    # But it stays visible: knowing it is probably another NileRed video is
    # the most useful thing the queue can say about it.
    assert after["candidates"][0]["name"] == "NileRed"


def test_impure_slots_are_refused_for_automatic_enrolment(db):
    pid = db.person_id_for("Alice")
    r = db.add_voiceprint(pid, vec(4), origin="auto", impure=True)
    assert not r["ok"] and r["reason"] == "impure"
    # A person listening may still say who it is.
    assert db.add_voiceprint(pid, vec(4), origin="manual", impure=True)["ok"]


def test_writes_invalidate_the_reference_cache(db):
    """Naming somebody must affect the very next match.

    match() caches the stacked reference matrix because rematching the archive
    rebuilt it sixteen thousand times. If a write does not invalidate it, a
    new name has no effect until something else happens to bump it -- which
    presents as the interface simply not working.
    """
    v = vec(5)
    assert db.match(v)["decision"] == "none"      # empty store, and now cached
    pid = db.person_id_for("Alice")
    db.add_voiceprint(pid, v)
    assert db.match(v)["name"] == "Alice", "cache outlived the write"

    db.delete_person(pid)
    assert db.match(v)["decision"] == "none", "cache outlived the delete"


def test_unknown_clusters_hide_media_but_can_be_asked_for_it(db):
    a = db.new_person()
    b = db.new_person()
    db.add_voiceprint(a, vec(6), origin="auto")
    db.add_voiceprint(b, vec(7), origin="auto")
    db.set_kind(b, db.KIND_MEDIA)

    assert [c["id"] for c in db.unknown_clusters()] == [a]
    assert {c["id"] for c in db.unknown_clusters(include_media=True)} == {a, b}


def test_naming_a_cluster_is_undoable(db):
    """Naming attaches every voiceprint in a cluster at once, so it needs a
    way back that does not mean deleting them one at a time."""
    cluster = db.new_person()
    for i in range(5):
        db.add_voiceprint(cluster, vec(10 + i), origin="auto")
    alice = db.person_id_for("Alice")
    db.add_voiceprint(alice, vec(20), origin="manual")

    r = db.name_person(cluster, "Alice")
    assert r["ok"] and r["merged"]
    groups = db.voiceprint_groups(alice)
    assert groups["total"] == 6
    assert [g["count"] for g in groups["groups"]] == [5]
    assert groups["singles_total"] == 1, "the hand-made one stays itemised"

    back = db.unname_group(alice, cluster)
    assert back["ok"] and back["moved"] == 5
    assert db.voiceprint_groups(alice)["total"] == 1
    assert any(c["prints"] == 5 for c in db.unknown_clusters())


def test_a_lone_person_falls_back_to_the_floor(db):
    """With nobody to be a runner-up the margin is undefined, and the
    arithmetic must not be left to decide what that means."""
    pid = db.person_id_for("Alice")
    v = vec(8)
    db.add_voiceprint(pid, v)

    strong = db.match(near(v, 0.95))
    assert strong["margin"] is None
    assert strong["decision"] == "matched"

    weak = db.match(vec(9))
    assert weak["margin"] is None
    assert weak["decision"] == "none"


def test_a_dissimilar_sample_is_accepted(db):
    """The old store refused a sample scoring below 0.55 against the person
    already enrolled. That rejected precisely the different-room, different-day
    samples this design exists to collect."""
    pid = db.person_id_for("Alice")
    db.add_voiceprint(pid, vec(30))
    r = db.add_voiceprint(pid, vec(31))        # nothing like the first
    assert r["ok"]
    assert len(db.voiceprints(pid)) == 2


def test_garbage_vectors_are_still_refused(db):
    pid = db.person_id_for("Alice")
    assert not db.add_voiceprint(pid, np.zeros(256))["ok"]
    assert not db.add_voiceprint(pid, np.full(256, np.nan))["ok"]
    assert not db.add_voiceprint(pid, np.array([]))["ok"]


def test_too_short_is_refused_with_a_reason(db):
    pid = db.person_id_for("Alice")
    r = db.add_voiceprint(pid, vec(40), seconds=1.0)
    assert not r["ok"] and r["reason"] == "too_short"
    assert db.add_voiceprint(pid, vec(40), seconds=30.0)["ok"]
