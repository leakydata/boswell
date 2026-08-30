#!/usr/bin/env python3
"""
Speaker identity: one row per voiceprint, never an average.

The old store kept one centroid per person -- every sample they had ever
contributed, averaged into a single vector. That is the wrong shape for this
problem. A voice measured in an empty room and the same voice measured
outdoors are genuinely far apart in embedding space, and averaging them
produces a vector that matches neither well. The little cross-condition
coverage that existed was destroyed at write time, and a threshold picked to
suppress false names then also rejected true ones.

So: every enrolment is kept as its own row, and matching takes the best single
reference rather than the mean of all of them. A person accumulates as many
voiceprints as they have conditions -- room, outdoors, tired, close mic -- and
any one of them is enough to recognise them in that condition.

Two consequences worth stating, because they drove the design:

  * Near-duplicate references are harmless. Matching is a max over rows, and
    max({a, a', b}) == max({a, b}) when a and a' are nearly equal. Duplicates
    cost storage and nothing else, so nothing needs to be merged for
    correctness. `redundant` exists to keep the labelling UI tidy, and rows
    flagged with it are still matched against. Nothing is ever deleted to
    save space.

  * The margin has to be between PEOPLE, not between rows. A well-covered
    person owns the top several rows, so a raw best-minus-runner-up margin
    collapses toward zero exactly where coverage is best -- it would reject
    the matches it was written to protect. Group by person, take each
    person's best row, then compare the top two people.

Unidentified voices are people rows with a NULL name. That makes naming a
stranger a single UPDATE: every voiceprint already gathered under that cluster
becomes a labelled reference at once, with no vectors moved or recomputed.
"""

import os
import sqlite3
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
DB = os.path.join(DATA, "speakers.db")

# Legacy files, read once by migrate() and then left alone.
LEGACY_CENTROIDS = os.path.join(DATA, "speakers.npz")
LEGACY_SAMPLES = os.path.join(DATA, "speaker_samples.npz")
LEGACY_META = os.path.join(DATA, "speakers.json")

# ---------------------------------------------------------------------------
# Thresholds.
#
# DERIVED, at last, rather than guessed. Everything here used to rest on one
# comparison -- five enrolment samples six minutes apart, scored against a
# centroid built from those same samples, which is circular -- and a
# different-speaker figure of 0.650 that came from three pairs.
#
# Diarizing the archive produced the real thing. Turns inside one slot are the
# same person under identical conditions; turns in two slots of one window are
# different people under identical conditions. From 1642 and 2141 pairs:
#
#     same person       p10 0.715   median 0.863   p90 0.937
#     different people              median 0.107   p90 0.288   p99 0.572
#
# The gap is 0.76, not the 0.16 this codebase was built around. The embedder
# separates people far better than anything here believed.
#
# Both figures read better than reality and the report says so: conditions are
# held constant, and the same-person side is drawn only from slots that passed
# the purity check, which a slot passes by having turns that agree. So these
# are treated as an optimistic bound, and the settings below sit well inside
# them rather than at the edge.
MATCH_HIGH = 0.75
# 0.80 named nothing. Not "little" -- zero of 196 clusters in the archive,
# because it sits above where genuine cross-condition matches actually land:
# the two clusters that are almost certainly the wearer score 0.778 and 0.790.
# 0.75 clears the different-speaker 99th percentile of 0.572 by 0.18 and
# captures 96 of the 98 minutes available to be named. Below 0.70 the extra
# clusters bring almost no additional speech and a good deal more risk.

MATCH_LOW = 0.55
# Anything above this is worth a person's glance. It sits just under the
# different-speaker p99, so roughly one different-speaker pair in a hundred
# reaches the queue -- which is the right way round for something a human
# reviews rather than something that acts on its own.

MARGIN_MIN = 0.15
# Raised from 0.06 now that the distributions are known. Different people sit
# at 0.107, so a genuine match wins by a mile and an ambiguous one barely wins
# at all -- measured on this archive, the confident clusters have margins of
# 0.51, 0.39 and 0.35 while the doubtful ones have 0.014 and 0.045. A bar of
# 0.15 falls in the empty space between those two groups and does the work the
# absolute score cannot.

MIN_ENROLL_SECONDS = 5.0

# Only rejects obvious garbage -- a silent or degenerate vector. It is
# deliberately NOT a resemblance test: the old store refused any sample scoring
# below 0.55 against the person already enrolled, which rejected precisely the
# different-room, different-day samples this design exists to collect.
MIN_VECTOR_NORM = 1e-6

SCHEMA = """
CREATE TABLE IF NOT EXISTS people (
    id      INTEGER PRIMARY KEY,
    name    TEXT UNIQUE,              -- NULL = an unidentified recurring voice
    kind    TEXT,                     -- person | media | NULL (not yet decided)
    created REAL
);

CREATE TABLE IF NOT EXISTS voiceprints (
    id        INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    vec       BLOB NOT NULL,          -- float32, unit length
    dim       INTEGER NOT NULL,
    seconds   REAL,                   -- speech behind this voiceprint
    clip      TEXT,
    speaker   TEXT,                   -- diarizer label it came from
    origin    TEXT NOT NULL,          -- manual | confirmed | auto | legacy
    redundant INTEGER NOT NULL DEFAULT 0,
    -- Pooled from a diarizer slot that disagreed with itself, so possibly a
    -- blend of two voices. Kept, shown and nameable by hand; never mixed with
    -- references that are not.
    impure    INTEGER NOT NULL DEFAULT 0,
    created   REAL
);
CREATE INDEX IF NOT EXISTS vp_person ON voiceprints(person_id);

-- Evidence for pruning. A harmful reference looks perfectly normal in vector
-- space, so distance cannot find it; only its record of winning matches that
-- were later corrected can. There is no way to reconstruct this after the
-- fact, so it is written from the start even though nothing reads it yet.
CREATE TABLE IF NOT EXISTS matches (
    id            INTEGER PRIMARY KEY,
    clip          TEXT,
    speaker       TEXT,
    voiceprint_id INTEGER,            -- the reference that won
    person_id     INTEGER,
    score         REAL,
    margin        REAL,
    decision      TEXT,               -- matched | uncertain | none
    corrected     INTEGER NOT NULL DEFAULT 0,
    created       REAL
);
CREATE INDEX IF NOT EXISTS match_vp ON matches(voiceprint_id);
CREATE INDEX IF NOT EXISTS match_clip ON matches(clip);
"""


def _conn():
    os.makedirs(DATA, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(people)")}
    if "kind" not in cols:
        c.execute("ALTER TABLE people ADD COLUMN kind TEXT")
        c.commit()
    vcols = {r["name"] for r in c.execute("PRAGMA table_info(voiceprints)")}
    if "source_cluster" not in vcols:
        c.execute("ALTER TABLE voiceprints ADD COLUMN source_cluster INTEGER")
        c.commit()
    if "impure" not in vcols:
        c.execute("ALTER TABLE voiceprints ADD COLUMN impure INTEGER "
                  "NOT NULL DEFAULT 0")
        c.commit()
    return c


# ---------------------------------------------------------------- vectors

def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


def _pack(v):
    return unit(v).astype(np.float32).tobytes()


def _unpack(blob):
    return np.frombuffer(blob, dtype=np.float32).astype(np.float64)


def is_usable(vec):
    """Whether a vector is worth storing at all. Not a resemblance test."""
    v = np.asarray(vec, dtype=np.float64).ravel()
    if v.size == 0 or not np.all(np.isfinite(v)):
        return False
    return float(np.linalg.norm(v)) > MIN_VECTOR_NORM


# ---------------------------------------------------------------- reading

# The reference matrix, cached.
#
# _rematch_clips scores every speaker in every transcript -- roughly sixteen
# thousand calls on this archive -- and each one was reloading every voiceprint
# out of SQLite and restacking it into a matrix. Naming somebody took ten
# seconds of that, during which the interface simply sat there.
#
# Invalidated by a counter the writes bump, not by a timer, so a stale matrix
# cannot outlive the change that made it stale.
_cache = {"version": None, "ids": None, "pids": None, "M": None}
_version = 0


def _invalidates(fn):
    """Any write makes the cached matrix wrong; say so once, at the door."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **kw):
        try:
            return fn(*a, **kw)
        finally:
            _bump()
    return wrap


def _bump():
    global _version
    _version += 1


def _references(c):
    """Every stored voiceprint as (ids, person_ids, matrix).

    Redundant rows are included: flagging one is a statement about the
    labelling UI, not about matching, and excluding them could only ever lose
    a match.
    """
    # Named people only. Unnamed clusters are not candidates for a NAME --
    # they are the question, not the answer. Ranking them here once let a
    # voice "match" a stranger and be dropped as settled instead of joining
    # that stranger's cluster. Clustering is _best_unknown's job.
    if _cache["version"] == _version:
        return _cache["ids"], _cache["pids"], _cache["M"]
    rows = c.execute("""
        SELECT v.id, v.person_id, v.vec FROM voiceprints v
        JOIN people p ON p.id = v.person_id
        WHERE p.name IS NOT NULL
        ORDER BY v.id
    """).fetchall()
    if not rows:
        out = ([], [], np.zeros((0, 0)))
    else:
        out = ([r["id"] for r in rows], [r["person_id"] for r in rows],
               np.stack([_unpack(r["vec"]) for r in rows]))
    _cache.update(version=_version, ids=out[0], pids=out[1], M=out[2])
    return out


def people(c=None):
    """Everyone in the store, named or not, with their voiceprint counts."""
    own = c is None
    c = c or _conn()
    try:
        rows = c.execute("""
            SELECT p.id, p.name, p.kind, p.created,
                   COUNT(v.id) AS prints,
                   COALESCE(SUM(v.seconds), 0) AS seconds
            FROM people p LEFT JOIN voiceprints v ON v.person_id = p.id
            GROUP BY p.id
            ORDER BY p.name IS NULL, p.name
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


def voiceprints(person_id, c=None):
    own = c is None
    c = c or _conn()
    try:
        rows = c.execute("""
            SELECT id, person_id, dim, seconds, clip, speaker, origin,
                   redundant, created
            FROM voiceprints WHERE person_id = ? ORDER BY created
        """, (person_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- matching

def match(vec, c=None):
    """Score one voiceprint against every reference.

    Returns the ranked candidates and a decision. Three outcomes, not two --
    "I do not know" is a correct answer and the one that feeds the labelling
    queue:

      matched    a person clears MATCH_HIGH and beats the next person by
                 MARGIN_MIN. Safe to apply without being asked.
      uncertain  somebody is plausible but not clear of the field. This is
                 where the useful manual labelling lives: a near-miss shown
                 with its score is confirmable in one click, and every
                 confirmation becomes a new reference covering a condition
                 that was not covered before.
      none       nothing in the store is close. A new voice.
    """
    own = c is None
    c = c or _conn()
    try:
        if not is_usable(vec):
            return {"decision": "none", "candidates": [], "score": 0.0,
                    "margin": 0.0, "reason": "unusable vector"}
        v = unit(vec)
        ids, pids, M = _references(c)
        if not ids or M.shape[1] != v.size:
            reason = "no references stored" if not ids else "dimension mismatch"
            return {"decision": "none", "candidates": [], "score": 0.0,
                    "margin": 0.0, "reason": reason}

        scores = M @ v

        # Best row per person, remembering which row it was so the match log
        # can name the reference that actually won.
        best = {}
        for pid, vid, s in zip(pids, ids, scores):
            if pid not in best or s > best[pid][0]:
                best[pid] = (float(s), vid)

        rows = c.execute("SELECT id, name, kind FROM people").fetchall()
        names = {r["id"]: r["name"] for r in rows}
        kinds = {r["id"]: r["kind"] for r in rows}
        ranked = sorted(
            ({"person_id": pid, "name": names.get(pid),
              "kind": kinds.get(pid), "score": round(s, 4),
              "voiceprint_id": vid} for pid, (s, vid) in best.items()),
            key=lambda d: -d["score"])

        top = ranked[0]
        runner = ranked[1]["score"] if len(ranked) > 1 else None
        margin = top["score"] - runner if runner is not None else None

        # With one person in the store there is no runner-up, so the margin is
        # undefined. Say so rather than letting the arithmetic decide: this is
        # the regime where a false name is easiest to create and hardest to
        # notice, so the absolute floor carries the decision alone and it is
        # deliberately the strict one.
        if margin is None:
            decision = "matched" if top["score"] >= MATCH_HIGH else (
                "uncertain" if top["score"] >= MATCH_LOW else "none")
        elif top["score"] >= MATCH_HIGH and margin >= MARGIN_MIN:
            decision = "matched"
        elif top["score"] >= MATCH_LOW:
            decision = "uncertain"
        else:
            decision = "none"

        # Media never names anything by itself.
        #
        # Dropping media out of the reference set entirely was the first
        # attempt and it was worse: the queue stopped saying "this is probably
        # another Network Chuck video", which is the single most useful thing
        # it can say about a voice off a screen. The problem was never that
        # media makes a bad candidate -- it makes an excellent one. It is that
        # nothing should acquire a name automatically from a voice that came
        # out of a speaker. So it stays visible, stays one click from being
        # confirmed, and is capped at "uncertain".
        if decision == "matched" and top.get("kind") == KIND_MEDIA:
            decision = "uncertain"

        return {"decision": decision,
                "candidates": ranked[:3],
                "score": top["score"],
                "margin": round(margin, 4) if margin is not None else None,
                "person_id": top["person_id"] if decision != "none" else None,
                "name": top["name"] if decision == "matched" else None,
                "kind": top.get("kind"),
                "voiceprint_id": top["voiceprint_id"]}
    finally:
        if own:
            c.close()


def log_match(clip, speaker, result, c=None):
    """Record what won, for the pruning pass that cannot be built yet."""
    own = c is None
    c = c or _conn()
    try:
        c.execute("""INSERT INTO matches
                     (clip, speaker, voiceprint_id, person_id, score, margin,
                      decision, created)
                     VALUES (?,?,?,?,?,?,?,?)""",
                  (clip, speaker, result.get("voiceprint_id"),
                   result.get("person_id"), result.get("score"),
                   result.get("margin"), result.get("decision"), time.time()))
        c.commit()
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- writing

def person_id_for(name, c=None, create=True):
    own = c is None
    c = c or _conn()
    try:
        r = c.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        if r:
            return r["id"]
        if not create:
            return None
        cur = c.execute("INSERT INTO people (name, created) VALUES (?,?)",
                        (name, time.time()))
        c.commit()
        return cur.lastrowid
    finally:
        if own:
            c.close()


@_invalidates
def add_voiceprint(person_id, vec, seconds=None, clip=None, speaker=None,
                   origin="manual", impure=False, c=None):
    """Store one reference. No resemblance check -- that is the whole point.

    A sample that scores badly against everything already stored for this
    person is the valuable one: it covers a condition nothing else covers.
    The only gate is whether the vector is usable at all.
    """
    own = c is None
    c = c or _conn()
    try:
        if not is_usable(vec):
            return {"ok": False, "reason": "unusable",
                    "detail": "the voiceprint is empty or not finite"}
        # A voiceprint pooled over a slot that disagreed with itself is a blend
        # of two voices, indistinguishable from a real one afterwards -- normal
        # length, normal neighbours, and wrong. So it is refused as a reference
        # for somebody with a NAME, which is where the damage would be done.
        #
        # It is allowed under an unnamed cluster, because a cluster asserts
        # nothing about who anybody is. Refusing it there was a mistake with a
        # measurable cost: 132 of the 137 voices recorded in one day were
        # impure, so they never became clusters, never reached the labelling
        # queue, and the interface reported nothing to do after a full day of
        # recording. The check was meant to stop a bad reference being learned
        # automatically, not to hide a fifth of the archive from the person who
        # could actually identify it.
        named = c.execute("SELECT name FROM people WHERE id = ?",
                          (person_id,)).fetchone()
        if impure and origin == "auto" and named and named["name"]:
            return {"ok": False, "reason": "impure",
                    "detail": "this voice could not be told apart from another "
                              "in the same recording; naming it by hand still "
                              "works, but it will not be learned automatically"}
        if seconds is not None and seconds < MIN_ENROLL_SECONDS:
            return {"ok": False, "reason": "too_short",
                    "seconds": round(seconds, 1),
                    "minimum": MIN_ENROLL_SECONDS,
                    "detail": f"only {seconds:.1f}s of this voice; "
                              f"{MIN_ENROLL_SECONDS:.0f}s or more makes a "
                              f"reliable voiceprint"}
        v = unit(vec)
        cur = c.execute("""INSERT INTO voiceprints
                           (person_id, vec, dim, seconds, clip, speaker,
                            origin, impure, created)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (person_id, _pack(v), int(v.size), seconds, clip,
                         speaker, origin, 1 if impure else 0, time.time()))
        c.commit()
        return {"ok": True, "voiceprint_id": cur.lastrowid,
                "person_id": person_id}
    finally:
        if own:
            c.close()


@_invalidates
def name_person(person_id, name, c=None):
    """Name an unidentified cluster, or rename someone.

    Every voiceprint already gathered under this person becomes a labelled
    reference at once. Nothing moves; it is one UPDATE.
    """
    own = c is None
    c = c or _conn()
    try:
        existing = c.execute("SELECT id FROM people WHERE name = ?",
                             (name,)).fetchone()
        if existing and existing["id"] != person_id:
            # Naming a stranger as somebody already known is a merge, and it
            # is the good case: their references join that person's set and
            # cover conditions that were missing.
            c.execute("UPDATE voiceprints SET person_id = ?, "
                      "source_cluster = COALESCE(source_cluster, ?) "
                      "WHERE person_id = ?",
                      (existing["id"], person_id, person_id))
            c.execute("DELETE FROM people WHERE id = ?", (person_id,))
            c.commit()
            return {"ok": True, "person_id": existing["id"], "merged": True}
        c.execute("UPDATE people SET name = ? WHERE id = ?", (name, person_id))
        c.execute("UPDATE voiceprints SET source_cluster = COALESCE(source_cluster, ?) "
                  "WHERE person_id = ?", (person_id, person_id))
        c.commit()
        return {"ok": True, "person_id": person_id, "merged": False}
    finally:
        if own:
            c.close()


@_invalidates
def new_person(name=None, c=None):
    own = c is None
    c = c or _conn()
    try:
        cur = c.execute("INSERT INTO people (name, created) VALUES (?,?)",
                        (name, time.time()))
        c.commit()
        return cur.lastrowid
    finally:
        if own:
            c.close()


@_invalidates
def delete_voiceprint(vp_id, c=None):
    own = c is None
    c = c or _conn()
    try:
        cur = c.execute("DELETE FROM voiceprints WHERE id = ?", (vp_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        if own:
            c.close()


@_invalidates
def delete_person(person_id, c=None):
    own = c is None
    c = c or _conn()
    try:
        cur = c.execute("DELETE FROM people WHERE id = ?", (person_id,))
        c.commit()
        return cur.rowcount > 0
    finally:
        if own:
            c.close()


@_invalidates
def set_redundant(vp_id, flag=True, c=None):
    """Hide a near-duplicate from the labelling UI without dropping it.

    Deliberately not a delete: matching still uses the row, and a merge you
    later decide was wrong cannot be undone if the vector is gone.
    """
    own = c is None
    c = c or _conn()
    try:
        c.execute("UPDATE voiceprints SET redundant = ? WHERE id = ?",
                  (1 if flag else 0, vp_id))
        c.commit()
    finally:
        if own:
            c.close()


def voiceprint_groups(person_id, c=None, detail_limit=12):
    """A person's references, described in groups rather than listed flat.

    Naming one cluster can attach several hundred voiceprints at once. Listing
    those individually is useless -- nobody audits three hundred rows, and the
    handful of hand-made labels that actually warrant attention get lost among
    them. So references that arrived together are described together, and only
    the ones a person made by hand are itemised.
    """
    own = c is None
    c = c or _conn()
    try:
        rows = c.execute("""
            SELECT id, seconds, clip, speaker, origin, redundant,
                   source_cluster, created
            FROM voiceprints WHERE person_id = ? ORDER BY created
        """, (person_id,)).fetchall()
        singles, groups = [], {}
        for r in rows:
            src = r["source_cluster"]
            # References acquired automatically arrived by naming a cluster,
            # whether or not the cluster id was recorded at the time. Grouping
            # them by origin as well as by id means the ones named before this
            # was tracked are still describable and still undoable, rather than
            # three hundred anonymous rows nobody will ever audit.
            if src is None and r["origin"] == "auto":
                src = 0
            if src is None:
                singles.append(dict(r))
                continue
            g = groups.setdefault(src, {"source_cluster": src, "count": 0,
                                        "seconds": 0.0, "clips": set(),
                                        "untracked": src == 0,
                                        "first": r["created"]})
            g["count"] += 1
            g["seconds"] += r["seconds"] or 0
            if r["clip"]:
                g["clips"].add(r["clip"])
        out = []
        for g in groups.values():
            g["clips"] = len(g["clips"])
            g["seconds"] = round(g["seconds"], 1)
            out.append(g)
        out.sort(key=lambda g: -g["count"])
        return {"groups": out,
                "singles": singles[-detail_limit:],
                "singles_total": len(singles),
                "total": len(rows)}
    finally:
        if own:
            c.close()


@_invalidates
def unname_group(person_id, source_cluster, c=None):
    """Undo a cluster naming: put those references back where they came from.

    Naming is one click and can be wrong about several hundred voiceprints at
    once, so it needs an equally cheap way back. The references are not deleted
    -- they return to being an unnamed cluster, exactly as they were, and can
    be named again as somebody else.
    """
    own = c is None
    c = c or _conn()
    try:
        if source_cluster == 0:
            where, args = ("person_id = ? AND source_cluster IS NULL "
                           "AND origin = 'auto'"), (person_id,)
        else:
            where, args = "person_id = ? AND source_cluster = ?", \
                          (person_id, source_cluster)
        n = c.execute(f"SELECT COUNT(*) n FROM voiceprints WHERE {where}",
                      args).fetchone()["n"]
        if not n:
            return {"ok": False, "reason": "no such group"}
        # Reuse the original cluster id where it is still free, so undoing
        # lands the voices back under the number the user saw them under.
        exists = source_cluster == 0 or c.execute(
            "SELECT id FROM people WHERE id = ?", (source_cluster,)).fetchone()
        if exists:
            target = c.execute("INSERT INTO people (name, created) VALUES (NULL, ?)",
                               (time.time(),)).lastrowid
        else:
            c.execute("INSERT INTO people (id, name, created) VALUES (?, NULL, ?)",
                      (source_cluster, time.time()))
            target = source_cluster
        c.execute(f"UPDATE voiceprints SET person_id = ?, source_cluster = NULL "
                  f"WHERE {where}", (target,) + args)
        # A person with nothing left is not a person any more.
        left = c.execute("SELECT COUNT(*) n FROM voiceprints WHERE person_id = ?",
                         (person_id,)).fetchone()["n"]
        if not left:
            c.execute("DELETE FROM people WHERE id = ?", (person_id,))
        c.commit()
        return {"ok": True, "moved": n, "cluster": target,
                "person_removed": not left}
    finally:
        if own:
            c.close()


# ------------------------------------------------------- unidentified voices

# How close two unknown voices must be to be filed as the same stranger.
#
# Deliberately stricter than MATCH_HIGH. Naming a cluster names every voice in
# it at once, so a cluster that has quietly merged two people puts one name on
# both -- and unlike a bad single match, that error arrives pre-multiplied.
#
# Know what this does and does not do. On this archive, voiceprints taken
# minutes apart score far higher than the same voice taken days apart, so
# clustering at any usable radius groups a voice WITHIN a recording session and
# largely fails to link it across sessions. A stranger who turns up on Tuesday
# and again on Friday will most likely appear as two clusters, not one. That is
# a real limit, not a tuning problem, and it is why naming is worth doing even
# though it is manual: a name, once given, is matched against directly and does
# not decay the way clustering does.
CLUSTER_MIN = 0.75

# A voice that came out of a speaker rather than a person in the room.
#
# These are most of the archive by volume and almost none of it by value, but
# deleting them would be wrong twice over. Their transcripts are worth keeping
# -- half the reason to have a recording of your day is finding the thing you
# watched and half remember. And they are the only cross-condition ground truth
# there is: one creator across dozens of videos is the same voice through the
# same microphone on different days in different rooms, which is exactly the
# data every threshold in this file is currently guessing without.
#
# What they must not do is compete for identity. A YouTube narrator on sapphire
# and 355 nm lasers sat under a real person's name here for weeks. Tagging is
# how that stops: a media voice keeps everything except its claim to be
# somebody.
KIND_PERSON = "person"
KIND_MEDIA = "media"


def unknown_clusters(c=None, include_media=False):
    """Recurring voices nobody has named, biggest first.

    Media is excluded by default. Knowing a voice came off a screen is a
    complete answer about it -- there is nothing further to decide, and leaving
    them in the queue would bury the handful of real people in a day's worth of
    videos.
    """
    own = c is None
    c = c or _conn()
    try:
        where = "p.name IS NULL" if include_media else \
                f"p.name IS NULL AND (p.kind IS NULL OR p.kind != '{KIND_MEDIA}')"
        rows = c.execute(f"""
            SELECT p.id, p.kind, COUNT(v.id) AS prints,
                   COALESCE(SUM(v.seconds), 0) AS seconds,
                   COUNT(DISTINCT v.clip) AS clips,
                   MIN(v.created) AS first_seen, MAX(v.created) AS last_seen
            FROM people p JOIN voiceprints v ON v.person_id = p.id
            WHERE {where}
            GROUP BY p.id
            ORDER BY seconds DESC, prints DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


@_invalidates
def set_kind(person_id, kind, c=None):
    """Say what sort of voice this is. A name is not required.

    This is the whole point of separating kind from name: you can recognise
    that something came out of a speaker without having any idea who was
    talking, and that is a complete and useful answer. Tagging it media retires
    it from the queue, stops it competing for identity, and keeps its words
    searchable.
    """
    if kind not in (KIND_PERSON, KIND_MEDIA, None):
        raise ValueError(f"kind must be person, media or None -- got {kind!r}")
    own = c is None
    c = c or _conn()
    try:
        cur = c.execute("UPDATE people SET kind = ? WHERE id = ?",
                        (kind, person_id))
        c.commit()
        return cur.rowcount > 0
    finally:
        if own:
            c.close()


def _best_unknown(v, c, impure=False):
    """The unnamed cluster this voice most resembles, if any clears CLUSTER_MIN.

    Impure voices are matched only against other impure ones, and clean against
    clean. Mixing them was the harm the purity check exists to prevent -- a
    blend of two people chained into a clean cluster spreads across everything
    it lands near. Isolating each impure voice completely was the first attempt
    at that and traded one problem for another: one person across thirty clips
    became thirty one-clip entries, which is the wall of SPEAKER_00 the queue
    was built to replace.
    """
    rows = c.execute("""
        SELECT v.person_id, v.vec FROM voiceprints v
        JOIN people p ON p.id = v.person_id
        WHERE p.name IS NULL AND (p.kind IS NULL OR p.kind != ?)
          AND v.impure = ?
    """, (KIND_MEDIA, 1 if impure else 0)).fetchall()
    if not rows:
        return None, 0.0
    M = np.stack([_unpack(r["vec"]) for r in rows])
    if M.shape[1] != v.size:
        return None, 0.0
    scores = M @ v
    best = {}
    for r, s in zip(rows, scores):
        pid = r["person_id"]
        if pid not in best or s > best[pid]:
            best[pid] = float(s)
    pid, score = max(best.items(), key=lambda kv: kv[1])
    return (pid, score) if score >= CLUSTER_MIN else (None, score)


@_invalidates
def ingest_unknown(vec, clip=None, speaker=None, seconds=None,
                   isolate=False, c=None):
    """File one unidentified voice, joining a cluster or starting one.

    Stored with origin 'auto' -- but note these are attached to NOBODY. An
    automatic reference under a named person would be an error-amplifying
    loop: it is stored precisely because it matched nothing, which is equally
    the signature of a new condition and of a wrong label, and once stored it
    wins future matches and seeds more of itself. An automatic cluster carries
    no name, so there is no claim to be wrong about, and it becomes evidence
    only when a person confirms it.
    """
    own = c is None
    c = c or _conn()
    try:
        if not is_usable(vec):
            return {"ok": False, "reason": "unusable"}
        v = unit(vec)
        # An impure voice groups with other impure voices, never with clean
        # ones. It stays out of the clusters a name will be attached to, and
        # still gathers its own repeats so a person who turns up all afternoon
        # is one entry to listen to rather than thirty.
        pid, score = _best_unknown(v, c, impure=isolate)
        created = pid is None
        if created:
            pid = new_person(None, c)
        r = add_voiceprint(pid, v, seconds=seconds, clip=clip, speaker=speaker,
                           origin="auto", impure=isolate, c=c)
        if not r.get("ok"):
            return r
        return {"ok": True, "person_id": pid, "voiceprint_id": r["voiceprint_id"],
                "new_cluster": created, "score": round(score, 4)}
    finally:
        if own:
            c.close()


def seen_voices(c=None):
    """(clip, speaker) pairs already stored, so a rescan is idempotent."""
    own = c is None
    c = c or _conn()
    try:
        return {(r["clip"], r["speaker"]) for r in c.execute(
            "SELECT clip, speaker FROM voiceprints "
            "WHERE clip IS NOT NULL AND speaker IS NOT NULL").fetchall()}
    finally:
        if own:
            c.close()


def voice_locations(person_id, c=None):
    """Where this person's voice was heard: (clip, speaker) with seconds."""
    own = c is None
    c = c or _conn()
    try:
        rows = c.execute("""
            SELECT id, clip, speaker, seconds, created FROM voiceprints
            WHERE person_id = ? AND clip IS NOT NULL
            ORDER BY created
        """, (person_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------- migration

def migrate(c=None):
    """Import the npz/json store, once. Returns what was brought across.

    The old per-sample vectors survive and become individual references, which
    is exactly the shape this store wants. The centroids do not: a centroid is
    an average of samples that no longer exist separately, so it comes across
    as one legacy reference rather than being unpacked into the samples it was
    built from. That is lossy and cannot be helped -- the individual recordings
    behind those averages were discarded by an earlier design.
    """
    import json

    own = c is None
    c = c or _conn()
    try:
        if c.execute("SELECT COUNT(*) n FROM voiceprints").fetchone()["n"]:
            return {"migrated": False, "reason": "store is not empty"}

        meta = {}
        if os.path.exists(LEGACY_META):
            try:
                meta = json.load(open(LEGACY_META))
            except Exception:
                meta = {}

        samples = {}
        if os.path.exists(LEGACY_SAMPLES):
            d = np.load(LEGACY_SAMPLES)
            samples = {k: d[k] for k in d.files}

        centroids = {}
        if os.path.exists(LEGACY_CENTROIDS):
            d = np.load(LEGACY_CENTROIDS)
            centroids = {k: d[k] for k in d.files}

        n_people = n_prints = 0
        for name in sorted(set(meta) | set(centroids)):
            pid = person_id_for(name, c)
            n_people += 1
            entries = (meta.get(name) or {}).get("samples", [])
            stored_any = False
            for e in entries:
                v = samples.get(f"{name}||{e['id']}")
                if v is None or not is_usable(v):
                    continue
                origin = "legacy" if e.get("legacy") else "manual"
                c.execute("""INSERT INTO voiceprints
                             (person_id, vec, dim, seconds, clip, speaker,
                              origin, created)
                             VALUES (?,?,?,?,?,?,?,?)""",
                          (pid, _pack(v), int(unit(v).size), e.get("seconds"),
                           e.get("clip"), e.get("speaker"), origin,
                           time.time()))
                n_prints += 1
                stored_any = True
            # Only fall back to the centroid when no individual sample
            # survived, so a person with real samples does not also carry
            # their own average as a competing reference.
            if not stored_any and name in centroids and is_usable(centroids[name]):
                c.execute("""INSERT INTO voiceprints
                             (person_id, vec, dim, origin, created)
                             VALUES (?,?,?,?,?)""",
                          (pid, _pack(centroids[name]),
                           int(unit(centroids[name]).size), "legacy",
                           time.time()))
                n_prints += 1
        c.commit()
        return {"migrated": True, "people": n_people, "voiceprints": n_prints}
    finally:
        if own:
            c.close()


if __name__ == "__main__":
    import json as _json
    r = migrate()
    print(_json.dumps(r, indent=2))
    for p in people():
        label = p["name"] or f"(unidentified #{p['id']})"
        print(f"  {label}: {p['prints']} voiceprint(s), "
              f"{p['seconds']:.0f}s")
