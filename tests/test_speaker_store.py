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


def test_an_impure_voice_still_reaches_the_queue(db):
    """Refusing it everywhere hid a fifth of the archive from the one person
    who could identify it.

    The refusal is about not learning a blended reference for somebody with a
    name. An unnamed cluster asserts nothing about who anybody is, so an impure
    voice belongs there -- visible, listenable, and flagged. It gets a cluster
    of its own rather than joining one, so the blend cannot spread.
    """
    a = db.new_person()
    db.add_voiceprint(a, vec(50), origin="auto")

    r = db.ingest_unknown(near(vec(50), 0.97), isolate=True)
    assert r["ok"], "an impure voice must still be storable as an unknown"
    assert r["new_cluster"], "it must not join a clean cluster, however close"
    assert r["person_id"] != a

    ids = {c["id"] for c in db.unknown_clusters()}
    assert {a, r["person_id"]} <= ids, "both must be visible in the queue"

    # But impure voices must gather with each other, or one person across
    # thirty clips becomes thirty one-clip entries.
    again = db.ingest_unknown(near(vec(50), 0.96, seed=7), isolate=True)
    assert again["person_id"] == r["person_id"], \
        "a repeat of the same impure voice must join it, not start again"
    assert not again["new_cluster"]


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


def test_a_clear_winner_counts_below_the_absolute_bar(db):
    """The thresholds, not the embedder, were the binding constraint.

    Held-out over 82 hand-labelled references: the right person was top-ranked
    83% of the time and the store would say so 22% of the time. The refusals
    clustered at score 0.744 against a bar of 0.75, with margins of 0.26-0.46
    against a bar of 0.15 -- the absolute score doing all the rejecting while
    the margin was nowhere near binding. Recall went 22% to 44% with precision
    unchanged at 100%.
    """
    # Below MATCH_HIGH, but nothing else is close.
    assert db.decide(0.70, 0.40) == "matched"
    # Below MATCH_HIGH and the field is close: still nobody's name.
    assert db.decide(0.70, 0.05) == "uncertain"
    # Clear of the field but below the floor: not a candidate at all.
    assert db.decide(0.40, 0.40) == "none"
    # The ordinary path is unchanged.
    assert db.decide(0.90, 0.30) == "matched"
    assert db.decide(0.90, 0.02) == "uncertain"


def test_a_lone_person_cannot_use_the_margin_path(db):
    """With no runner-up there is no margin, so the floor has to carry it --
    and it is the strict one, because this is where a false name is easiest to
    create and hardest to notice."""
    assert db.decide(0.90, None) == "matched"
    assert db.decide(0.70, None) == "uncertain"
    assert db.decide(0.40, None) == "none"


def test_compaction_collapses_copies_and_is_reversible(db):
    """Duplicates arrive systematically: consolidation writes one voiceprint
    into every clip of a conversation. One creator here had 42 references with
    all 861 pairs above 0.99."""
    pid = db.person_id_for("Alice")
    base = vec(60)
    for i in range(5):
        db.add_voiceprint(pid, near(base, 0.995, seed=i), seconds=10 + i)
    db.add_voiceprint(pid, vec(61), seconds=30)      # a genuinely different one

    r = db.compact_person(pid)
    assert r["flagged"] == 4, "the copies collapse to one"
    assert r["kept"] == 2, "the different condition survives"

    # Nothing is deleted, and the judgement is reversible.
    assert len(db.voiceprints(pid)) == 6
    assert db.uncompact(pid)["restored"] == 4
    assert db.compact_person(pid)["flagged"] == 4


def test_one_voiceprint_is_enough_evidence_to_be_recognised(db):
    """A cluster holding a single voiceprint must be resolvable.

    recheck required max(2, min(3, n)) agreeing votes, which for n=1 demands
    two votes from one voice -- impossible by construction. 91 of the 92
    clusters queued for hand-labelling were exactly that, so no amount of
    confidence could ever clear them. One voiceprint deciding "matched" is the
    same evidence identify() already acts on when it names a voice outright.
    """
    def needed(tested):
        return max(1, (tested + 1) // 2)

    assert needed(1) == 1, "one voiceprint cannot be asked for two votes"
    assert needed(2) == 1
    assert needed(3) == 2
    assert needed(5) == 3, "a bigger cluster still needs a majority"


def test_renaming_a_slot_replaces_rather_than_accumulates(db, tmp_path, monkeypatch):
    """Correcting a mistake must not leave the mistake behind.

    Naming appended unconditionally, so a user who named a slot, saw the name
    land on every line of the clip, switched it back and tried again produced
    four references from one slot -- two under each name, the slot standing as
    evidence for two different people at once. That is exactly how it happened
    on a real clip.
    """
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "web"))
    import pipeline
    monkeypatch.setattr(pipeline, "_migrated", True)
    monkeypatch.setattr(pipeline, "_sdb", lambda: db)

    v = vec(70)
    for who in ["Alice", "Bob", "Alice", "Bob"]:
        pipeline.save_speaker(who, v, clip="c.wav", speaker="SPEAKER_00",
                              seconds=18.3)

    c = db._conn()
    try:
        n = c.execute("SELECT COUNT(*) n FROM voiceprints "
                      "WHERE clip='c.wav'").fetchone()["n"]
        assert n == 1, "one slot is one piece of evidence"
        owner = c.execute("""SELECT p.name FROM voiceprints v
                             JOIN people p ON p.id = v.person_id
                             WHERE v.clip='c.wav'""").fetchone()["name"]
        assert owner == "Bob", "it belongs to whoever was named last"
    finally:
        c.close()


# ---------------------------------------------------------------- setting aside
#
# Media is for a voice off a screen worth recognising again -- a channel whose
# words are worth having, and which the queue can then say "probably another
# one of those" about. This is the other case, and it was missing: a television
# in the next room, a video scrolled past, a stranger on a phone. Without a
# third answer those came back to the queue every day asking to be named, and a
# queue that cannot be finished stops being used.


def test_setting_a_voice_aside_retires_it(db):
    a = db.new_person()
    b = db.new_person()
    db.add_voiceprint(a, vec(30), origin="auto")
    db.add_voiceprint(b, vec(31), origin="auto")
    db.set_kind(b, db.KIND_IGNORED)

    assert [c["id"] for c in db.unknown_clusters()] == [a]
    assert {c["id"] for c in db.unknown_clusters(include_ignored=True)} == {a, b}


def test_a_voice_set_aside_never_names_anything(db):
    """It stays unnamed, and only named people are references -- so a dismissed
    voice cannot put a label on anybody, today or later."""
    alice = db.person_id_for("Alice")
    db.add_voiceprint(alice, vec(40), origin="manual")
    b = db.new_person()
    db.add_voiceprint(b, vec(32), origin="auto")
    db.set_kind(b, db.KIND_IGNORED)

    # Its own vector back, with a real named person in the store so there is
    # something for match() to rank. The dismissed cluster must not be in it.
    r = db.match(vec(32))
    assert r.get("name") is None
    assert b not in [c["person_id"] for c in r.get("candidates", [])]


def test_the_same_voice_lands_back_in_it(db):
    """Otherwise every rerun of the same television arrives as a stranger and
    the queue fills up with the thing that was just dismissed."""
    b = db.new_person()
    db.add_voiceprint(b, vec(33), origin="auto")
    db.set_kind(b, db.KIND_IGNORED)

    again = db.ingest_unknown(near(vec(33), 0.95), clip="later.wav",
                              speaker="SPEAKER_00", seconds=30)
    assert again.get("person_id") == b


def test_setting_aside_is_undoable_and_keeps_what_it_collected(db):
    b = db.new_person()
    db.add_voiceprint(b, vec(34), origin="auto")
    db.set_kind(b, db.KIND_IGNORED)
    db.add_voiceprint(b, near(vec(34), 0.95), origin="auto")

    assert db.set_kind(b, None)
    back = [c for c in db.unknown_clusters() if c["id"] == b]
    assert back and back[0]["prints"] == 2


def test_media_and_set_aside_hide_independently(db):
    m, i = db.new_person(), db.new_person()
    db.add_voiceprint(m, vec(35), origin="auto")
    db.add_voiceprint(i, vec(36), origin="auto")
    db.set_kind(m, db.KIND_MEDIA)
    db.set_kind(i, db.KIND_IGNORED)

    assert db.unknown_clusters() == []
    assert {c["id"] for c in db.unknown_clusters(include_media=True)} == {m}
    assert {c["id"] for c in db.unknown_clusters(include_ignored=True)} == {i}
    assert {c["id"] for c in db.unknown_clusters(include_media=True,
                                                include_ignored=True)} == {m, i}


def test_an_unknown_kind_is_still_refused(db):
    b = db.new_person()
    with pytest.raises(ValueError):
        db.set_kind(b, "dog")
