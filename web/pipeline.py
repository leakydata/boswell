#!/usr/bin/env python3
"""
Background transcription worker for the web UI.

Runs in a thread so the event loop keeps servicing Bluetooth while a clip is
transcribed. The ASR and diarization models are loaded once on the first job
and kept resident -- reloading them per clip would cost ~15 s every time.
"""

import json
import math
import os
import atomicio
import queue
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
TRANSCRIPTS = os.path.join(DATA, "transcripts")
SPEAKER_DB = os.path.join(DATA, "speakers.npz")
SPEAKER_META = os.path.join(DATA, "speakers.json")
# The single match threshold that used to live here is gone. It could not do
# the job: held out properly, the score distribution runs continuously from
# 0.85 down through 0.74 with no break wider than 0.009 anywhere in it, so
# there is no boundary to find and any one number is a choice about which
# error to make. speaker_store decides with a floor, a ceiling and a margin
# between people instead, and records what it decided so the numbers can be
# re-derived from evidence rather than guessed again.


def unit(v):
    v = np.asarray(v, dtype=np.float64).ravel()
    n = np.linalg.norm(v)
    return v / n if n else v


# The npz store the SQLite one was migrated from. Read once by
# speaker_store.migrate() and then left alone; kept so the import is repeatable
# and so nothing is destroyed by the move.
SAMPLES_DB = os.path.join(DATA, "speaker_samples.npz")


def load_speakers():
    """Legacy centroid view, kept only so the old npz store stays readable.

    Nothing in the matching path uses this any more -- see speaker_store, which
    holds one row per voiceprint and never averages them.
    """
    if not os.path.exists(SPEAKER_DB):
        return {}
    d = np.load(SPEAKER_DB)
    return {k: unit(d[k]) for k in d.files}


def _sdb():
    """The store, migrating the npz/json files across on first use."""
    import speaker_store
    speaker_store.migrate()
    return speaker_store


def list_speakers():
    """People and their individual voiceprints.

    Shaped like the old centroid store's output so the web UI did not have to
    change: `samples` is the list of references, and `count` is how many
    conditions this person is covered for -- which is what it now means.
    """
    sdb = _sdb()
    out = []
    for p in sdb.people():
        if p["name"] is None:
            continue          # unidentified clusters have their own view
        prints = sdb.voiceprints(p["id"])
        out.append({
            "name": p["name"],
            "person_id": p["id"],
            "kind": p.get("kind"),
            "samples": [{"id": str(v["id"]), "weight": 1,
                         "clip": v["clip"], "speaker": v["speaker"],
                         "seconds": round(v["seconds"], 1) if v["seconds"] else v["seconds"],
                         "origin": v["origin"],
                         "redundant": bool(v["redundant"]),
                         "score": None} for v in prints],
            "count": len(prints),
        })
    return out


def save_speaker(name, vec, clip=None, speaker=None, seconds=None, force=False):
    """Add one reference for a person.

    The old version refused any sample scoring below 0.55 against the person
    already enrolled, on the reasoning that a dissimilar sample poisons the
    average. There is no average any more, and that gate was rejecting exactly
    the samples worth having: the same voice in a different room, on a
    different day, through a different mic. A sample that resembles nothing
    stored is the one that adds coverage.

    What is still checked is whether the audio is worth learning from at all --
    too little speech, or a degenerate vector.
    """
    sdb = _sdb()
    if not force and seconds is not None and seconds < sdb.MIN_ENROLL_SECONDS:
        return {"ok": False, "reason": "too_short", "seconds": round(seconds, 1),
                "minimum": sdb.MIN_ENROLL_SECONDS,
                "detail": f"only {seconds:.1f}s of this voice in the clip; "
                          f"{sdb.MIN_ENROLL_SECONDS:.0f}s or more makes a "
                          f"reliable voiceprint"}
    if not sdb.is_usable(vec):
        return {"ok": False, "reason": "unusable",
                "detail": "no usable voiceprint was extracted for this speaker"}

    c = sdb._conn()
    try:
        pid = sdb.person_id_for(name, c)
        # What this sample scores against what is already stored, recorded for
        # information rather than used as a gate -- a low number here means new
        # coverage, which is the point.
        before = sdb.match(vec, c)
        novelty = None
        for cand in before.get("candidates", []):
            if cand["person_id"] == pid:
                novelty = cand["score"]
                break
        r = sdb.add_voiceprint(pid, vec, seconds=seconds, clip=clip,
                               speaker=speaker,
                               origin="confirmed" if novelty is not None else "manual",
                               c=c)
        if not r.get("ok"):
            return r
        n = len(sdb.voiceprints(pid, c))
        return {"ok": True, "name": name, "count": n,
                "person_id": pid, "voiceprint_id": r["voiceprint_id"],
                "score": novelty,
                "detail": (f"stored; it scores {novelty:.2f} against this "
                           f"person's existing references"
                           if novelty is not None else
                           "stored as the first reference for this person")}
    finally:
        c.close()


def delete_sample(name, sample_id):
    sdb = _sdb()
    try:
        return sdb.delete_voiceprint(int(sample_id))
    except (TypeError, ValueError):
        return False


def delete_speaker(name):
    sdb = _sdb()
    c = sdb._conn()
    try:
        pid = sdb.person_id_for(name, c, create=False)
        return sdb.delete_person(pid, c) if pid else False
    finally:
        c.close()


def identify(embeddings, clip=None):
    """Put names to diarized voices.

    Three outcomes rather than two. A voice that nobody can vouch for is left
    unnamed on purpose, and the near-misses are kept: `candidates` carries the
    top three with their scores, so a voice the matcher was not confident
    enough to name is still one click from being confirmed by someone who
    recognises it -- and every confirmation becomes a new reference.
    """
    sdb = _sdb()
    out = {}
    c = sdb._conn()
    try:
        for spk, vec in (embeddings or {}).items():
            r = sdb.match(vec, c)
            out[spk] = {"name": r.get("name"),
                        "score": round(r["score"], 3) if r["score"] else 0.0,
                        "decision": r["decision"],
                        "margin": r.get("margin"),
                        "candidates": r.get("candidates", [])}
            if clip:
                sdb.log_match(clip, spk, r, c)
    finally:
        c.close()
    return out


def voice_seconds(segments, speaker):
    """How much speech a diarized voice actually has in a clip."""
    return sum(float(x["end"]) - float(x["start"]) for x in segments
               if x.get("speaker") == speaker
               and isinstance(x.get("start"), (int, float))
               and isinstance(x.get("end"), (int, float)))


def scan_voices(limit=None, min_seconds=3.0):
    """File every diarized voice that nobody has accounted for yet.

    Walks the transcripts, skips voices already stored and voices a name has
    already been put to, and files the rest into unnamed clusters so a
    recurring stranger becomes one entry to name rather than a hundred
    disconnected SPEAKER_00s. Idempotent: rerunning it picks up only what is
    new, so it is safe to call after every transcription.
    """
    sdb = _sdb()
    c = sdb._conn()
    try:
        seen = sdb.seen_voices(c)
        stats = {"scanned": 0, "skipped_known": 0, "skipped_short": 0,
                 "skipped_impure": 0, "matched": 0, "clustered": 0,
                 "new_clusters": 0}
        files = sorted(f for f in os.listdir(TRANSCRIPTS)
                       if f.endswith(".json")) if os.path.isdir(TRANSCRIPTS) else []
        for f in files:
            try:
                t = json.load(open(os.path.join(TRANSCRIPTS, f)))
            except Exception:
                continue
            clip = t.get("clip") or (f[:-5] + ".wav")
            segs = t.get("segments") or []
            named = t.get("speakers") or {}
            for spk, vec in (t.get("embeddings") or {}).items():
                if (clip, spk) in seen:
                    stats["skipped_known"] += 1
                    continue
                # A voice somebody has already named by hand is settled.
                if (named.get(spk) or {}).get("name"):
                    stats["skipped_known"] += 1
                    continue
                # A slot the diarizer could not keep straight is a blend of
                # two voices. Clustering it would spread that blend across
                # everything it lands near, and nothing downstream could ever
                # tell -- so it waits for a person to listen instead.
                if (named.get(spk) or {}).get("impure"):
                    stats["skipped_impure"] += 1
                    continue
                secs = voice_seconds(segs, spk)
                if secs < min_seconds:
                    stats["skipped_short"] += 1
                    continue
                stats["scanned"] += 1
                r = sdb.match(vec, c)
                if r["decision"] == "matched":
                    stats["matched"] += 1
                    continue
                g = sdb.ingest_unknown(vec, clip=clip, speaker=spk,
                                       seconds=secs, c=c)
                if g.get("ok"):
                    stats["clustered"] += 1
                    stats["new_clusters"] += 1 if g.get("new_cluster") else 0
                    seen.add((clip, spk))
                if limit and stats["clustered"] >= limit:
                    return stats
        return stats
    finally:
        c.close()


def recheck_unknowns():
    """Re-test every unnamed cluster against the people who are known now.

    This is the loop the whole store is built around. Naming one voice adds
    references for conditions that had none, which raises the score of every
    other recording made under those conditions -- so a cluster that matched
    nobody an hour ago may match confidently once you have named someone.
    Without this the store only ever gets better for audio recorded after the
    naming, which is the wrong half of the archive.

    Only absorbs a cluster on a full `matched` decision -- clear of the floor
    and clear of the runner-up. Anything less stays in the queue for a person
    to settle, because a cluster absorbed wrongly puts one name on everything
    in it at once.
    """
    sdb = _sdb()
    c = sdb._conn()
    try:
        stats = {"checked": 0, "absorbed": 0, "voiceprints_moved": 0,
                 "into": {}}
        for cl in sdb.unknown_clusters(c):
            stats["checked"] += 1
            rows = c.execute(
                "SELECT id, vec, seconds FROM voiceprints WHERE person_id = ? "
                "ORDER BY seconds DESC", (cl["id"],)).fetchall()
            if not rows:
                continue

            # Decide on the cluster's best-evidenced voices rather than any
            # single one: a cluster is a claim that these are all the same
            # person, so a lone flattering vector should not carry it.
            votes = {}
            for r in rows[:5]:
                m = sdb.match(sdb._unpack(r["vec"]), c)
                if m["decision"] == "matched" and m.get("person_id"):
                    votes[m["person_id"]] = votes.get(m["person_id"], 0) + 1
            if not votes:
                continue
            pid, n = max(votes.items(), key=lambda kv: kv[1])
            if n < max(2, min(3, len(rows[:5]))):
                continue          # not agreed on by enough of the cluster

            name = c.execute("SELECT name FROM people WHERE id = ?",
                             (pid,)).fetchone()["name"]
            c.execute("UPDATE voiceprints SET person_id = ? WHERE person_id = ?",
                      (pid, cl["id"]))
            c.execute("DELETE FROM people WHERE id = ?", (cl["id"],))
            c.commit()
            stats["absorbed"] += 1
            stats["voiceprints_moved"] += len(rows)
            stats["into"][name] = stats["into"].get(name, 0) + len(rows)
        return stats
    finally:
        c.close()


def labelling_queue(limit=50, include_media=False):
    """Unnamed voices worth someone's attention, most speech first.

    Each entry carries what it would take to settle it in one look: how much
    was said, where, the words, and the closest named people with their
    scores. A near-miss shown with its score is confirmable in one click, and
    that confirmation is worth more than the label -- it becomes a reference
    covering a condition that had no coverage.
    """
    sdb = _sdb()
    c = sdb._conn()
    try:
        out = []
        for cl in sdb.unknown_clusters(c, include_media)[:limit]:
            locs = sdb.voice_locations(cl["id"], c)
            row = c.execute("SELECT vec FROM voiceprints WHERE person_id = ? "
                            "ORDER BY seconds DESC LIMIT 1", (cl["id"],)).fetchone()
            cands = []
            if row is not None:
                r = sdb.match(sdb._unpack(row["vec"]), c)
                cands = [x for x in r.get("candidates", []) if x["name"]]
            text = []
            for loc in locs[:6]:
                tp = transcript_path(loc["clip"])
                if not os.path.exists(tp):
                    continue
                try:
                    t = json.load(open(tp))
                except Exception:
                    continue
                said = " ".join(x["text"] for x in (t.get("segments") or [])
                                if x.get("speaker") == loc["speaker"])
                if said.strip():
                    text.append(said.strip())
            out.append({
                "person_id": cl["id"],
                "kind": cl.get("kind"),
                "seconds": round(cl["seconds"], 1),
                "voiceprints": cl["prints"],
                "clips": cl["clips"],
                "first_seen": cl["first_seen"],
                "last_seen": cl["last_seen"],
                "locations": locs,
                "candidates": cands[:3],
                "text": " … ".join(text)[:600],
            })
        return out
    finally:
        c.close()


VOCAB_PATH = os.path.join(DATA, "vocabulary.json")
# Below this, a fuzzy match is more likely to corrupt a correct word than to
# fix a wrong one.
FUZZY_CUTOFF = 0.82
MIN_FUZZY_LEN = 5


def load_vocabulary():
    """Domain words the model would otherwise mangle: drug names, project
    names, jargon, and the people you have enrolled."""
    terms = []
    if os.path.exists(VOCAB_PATH):
        try:
            terms = list(json.load(open(VOCAB_PATH)).get("terms", []))
        except Exception:
            terms = []
    # Enrolled names are exactly the words an ASR model gets wrong, so they
    # are always boosted without needing to be typed in twice.
    try:
        for p in list_speakers():
            if p["name"] not in terms:
                terms.append(p["name"])
    except Exception:
        pass
    return [t.strip() for t in terms if t and t.strip()]


def save_vocabulary(terms):
    os.makedirs(DATA, exist_ok=True)
    clean, seen = [], set()
    for t in terms:
        t = (t or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            clean.append(t)
    atomicio.write_json(VOCAB_PATH, {"terms": clean}, indent=2)
    return clean


def apply_vocabulary(text, terms):
    """Fix near-misses the decoder bias did not catch.

    Boosting makes a term likelier, it does not guarantee it, so "Metformin"
    can still land as "metformen" or, worse, be split into "met foreman".
    Two passes handle those separately: a phrase pass that re-joins runs of
    words, then a word pass. Only reasonably long terms are fuzzy-matched --
    a wrong correction on a short word does more damage than a missed one.
    """
    import difflib
    import re as _re

    if not terms:
        return text

    exact = {t.lower(): t for t in terms}
    letters = lambda w: _re.sub(r"[^a-z0-9]", "", w.lower())
    # Terms worth trying to reassemble from several words.
    joined = {letters(t): t for t in terms if len(letters(t)) >= MIN_FUZZY_LEN}

    token = _re.compile(r"[A-Za-z][A-Za-z0-9'-]*")

    # Pass 1: windows of 3 then 2 words, in case the term was split apart.
    for width in (3, 2):
        while True:
            toks = list(token.finditer(text))
            replaced = False
            for i in range(len(toks) - width + 1):
                span = toks[i:i + width]
                # Only merge words that are adjacent with plain spaces.
                between = text[span[0].end():span[-1].start()]
                if _re.search(r"[^ ]", between):
                    continue
                cand = letters("".join(m.group(0) for m in span))
                if len(cand) < MIN_FUZZY_LEN:
                    continue
                hit = difflib.get_close_matches(cand, joined.keys(), n=1, cutoff=0.87)
                if hit:
                    text = text[:span[0].start()] + joined[hit[0]] + text[span[-1].end():]
                    replaced = True
                    break
            if not replaced:
                break

    # Pass 2: single words.
    def fix(m):
        w = m.group(0)
        low = w.lower()
        if low in exact:
            return exact[low]
        key = letters(w)
        if key in joined:
            return joined[key]
        if len(w) < MIN_FUZZY_LEN:
            return w
        hit = difflib.get_close_matches(key, joined.keys(), n=1, cutoff=FUZZY_CUTOFF)
        return joined[hit[0]] if hit else w

    return token.sub(fix, text)


def transcript_path(clip):
    """Where a clip's transcript lives.

    Enforces a bare filename. This took whatever it was given and joined it,
    so a name carrying a path would have produced a transcript path outside
    the store -- the callers happened to validate first, which is not the
    same as this being safe to call.
    """
    name = os.path.basename(clip)
    if name != clip or not name:
        raise ValueError(f"clip name must be a bare filename: {clip!r}")
    return os.path.join(TRANSCRIPTS, os.path.splitext(name)[0] + ".json")


# Longest transcript that a clip with no detected voice is allowed to have
# before it is treated as real. Whisper's silence phrases are short; anything
# longer is more likely to be speech the diarizer merely failed to cluster.
HALLUCINATION_CHARS = 40


def _collect_calibration(acc, turn_vecs, trusted=None, max_pairs=400):
    """Harvest matched-condition pairs from a window that was just diarized.

    This is the measurement the whole store has been missing. Every threshold
    in speaker_store is a guess, and the reason it is a guess is that nobody
    has ever had a clean set of "same person" and "different person" scores off
    this hardware -- the one comparison on record was five enrolment samples
    six minutes apart against a centroid built from them, which is circular.

    Diarizing a conversation produces both, non-circularly and for free:

      same       two turns inside one slot. Same person by the diarizer's own
                 reckoning, same room, same microphone, same minute.
      different  two turns in different slots of the same window. Different
                 people, and every other condition held identical.

    The gap between those two distributions is the only honest basis for
    setting MATCH_HIGH and MARGIN_MIN. It is an upper bound on what is
    achievable -- conditions are held constant here and are not in real
    matching -- so a threshold derived from it should be treated as optimistic
    and checked against held-out labels once there are any.

    It also inherits the diarizer's mistakes: a slot holding two people files
    their scores under "same". That is what the purity check is for, and why
    the two are computed together.
    """
    ok = turn_vecs.keys() if trusted is None else trusted
    slots = [v for k, v in turn_vecs.items() if k in ok and len(v) >= 2]
    for vecs in slots:
        M = np.stack(vecs)
        sims = M @ M.T
        iu = np.triu_indices(len(vecs), k=1)
        acc["same"].extend(float(x) for x in sims[iu])
    keys = [k for k, v in turn_vecs.items() if len(v) >= 1]
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            A, B = np.stack(turn_vecs[a]), np.stack(turn_vecs[b])
            acc["different"].extend(float(x) for x in (A @ B.T).ravel())
    for k in ("same", "different"):
        if len(acc[k]) > max_pairs:
            step = len(acc[k]) / max_pairs
            acc[k] = [acc[k][int(i * step)] for i in range(max_pairs)]


def _save_calibration(acc):
    """Keep the pairs. They cannot be recomputed once the audio is rotated."""
    if not acc["same"] and not acc["different"]:
        return
    sdb = _sdb()
    c = sdb._conn()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS condition_pairs (
                         id INTEGER PRIMARY KEY,
                         kind TEXT NOT NULL,      -- same | different
                         score REAL NOT NULL,
                         created REAL)""")
        now = time.time()
        c.executemany("INSERT INTO condition_pairs (kind, score, created) "
                      "VALUES (?,?,?)",
                      [(k, v, now) for k in ("same", "different")
                       for v in acc[k]])
        c.commit()
    finally:
        c.close()


def _impure_rate():
    """How often the diarizer cannot keep a slot to one person.

    Reported alongside the thresholds because it bounds what any threshold can
    achieve: a reference pooled from a slot holding two voices is wrong before
    matching begins, and no cutoff recovers from that.
    """
    if not os.path.isdir(TRANSCRIPTS):
        return None
    # Every clip in a consolidated conversation carries the same slot_quality,
    # so counting per file counts one slot once per clip -- and because impure
    # slots turn up in the longer conversations, that weights the rate upward.
    # Key on the consolidation stamp to count each distinct slot once.
    seen = {}
    for f in os.listdir(TRANSCRIPTS):
        if not f.endswith(".json"):
            continue
        try:
            t = json.load(open(os.path.join(TRANSCRIPTS, f)))
        except Exception:
            continue
        for spk, v in (t.get("slot_quality") or {}).items():
            if v.get("coherence") is None:
                continue
            # Clips diarized together carry byte-identical quality for a slot,
            # so the measurement itself identifies the slot. This works on
            # transcripts written before consolidation used a shared stamp.
            seen[(spk, v.get("coherence"), v.get("worst_pair"),
                  v.get("turns_tested"))] = bool(v.get("suspect"))
    total = len(seen)
    suspect = sum(1 for v in seen.values() if v)
    return {"slots_measured": total, "impure": suspect,
            "rate": round(suspect / total, 3) if total else None,
            "note": "distinct slots, counted once per consolidated conversation"}


def calibration_report():
    """What the archive now knows about same-voice versus different-voice.

    Reports the two distributions and where they cross, so a threshold can be
    argued for rather than picked. Says nothing if there is not enough data --
    a number derived from forty pairs would be worse than admitting ignorance,
    because it would look like evidence.
    """
    sdb = _sdb()
    c = sdb._conn()
    try:
        try:
            rows = c.execute("SELECT kind, score FROM condition_pairs").fetchall()
        except Exception:
            return {"ready": False, "reason": "no measurements yet -- "
                                              "consolidate some conversations"}
        same = np.array([r["score"] for r in rows if r["kind"] == "same"])
        diff = np.array([r["score"] for r in rows if r["kind"] == "different"])
        if len(same) < 200 or len(diff) < 200:
            return {"ready": False, "same_pairs": len(same),
                    "different_pairs": len(diff),
                    "reason": "not enough measurements to be worth trusting; "
                              "consolidate more conversations"}

        def pct(a, q):
            return round(float(np.percentile(a, q)), 3)

        # Where a threshold would sit if you wanted to name almost nobody
        # wrongly: above nearly all different-speaker pairs.
        floor = pct(diff, 99)
        # And what that costs -- the share of genuine same-speaker pairs it
        # throws away.
        lost = float((same < floor).mean())
        return {
            "ready": True,
            "same_pairs": len(same), "different_pairs": len(diff),
            "same": {"p10": pct(same, 10), "median": pct(same, 50),
                     "p90": pct(same, 90)},
            "different": {"median": pct(diff, 50), "p90": pct(diff, 90),
                          "p99": pct(diff, 99)},
            "separation": round(pct(same, 50) - pct(diff, 50), 3),
            "suggested_floor": floor,
            "same_speech_rejected_at_that_floor": round(lost, 3),
            "current": {"MATCH_HIGH": sdb.MATCH_HIGH,
                        "MATCH_LOW": sdb.MATCH_LOW,
                        "MARGIN_MIN": sdb.MARGIN_MIN},
            "caveats": [
                # Two separate reasons this reads better than reality.
                "Conditions are held constant -- one room, one microphone, "
                "one moment. Real matching crosses rooms and days, which is "
                "the hard case and is not measured here.",
                # And a selection effect in the same-person side specifically.
                "The same-person pairs come only from slots that passed the "
                "purity check, and a slot passes precisely by having turns "
                "that agree. So the same-person distribution is selected for "
                "agreement and its lower tail is optimistic. The "
                "different-person side has no such filter and if anything "
                "runs the other way: an impure slot contributes some "
                "same-person pairs to it, which can only push it up.",
                "Treat the floor as an upper bound on what a threshold can "
                "safely be, not as a value to adopt. Re-derive against "
                "held-out human labels once there are enough.",
            ],
            "impure_slot_rate": _impure_rate(),
        }
    finally:
        c.close()


class Worker:
    def __init__(self, notify=None, on_transcript=None):
        self.q: queue.Queue = queue.Queue()
        self.notify = notify or (lambda *a, **k: None)
        # Called with a finished transcript so downstream consumers -- the
        # agent -- do not have to poll the filesystem for new work.
        self.on_transcript = on_transcript or (lambda *a, **k: None)
        self.busy = None
        # What is queued or in flight. Rotation, a single request, a whole
        # conversation and a bulk action can all name the same clip, and every
        # one of them used to mean another full ASR and diarization pass --
        # expensive, and a later pass could overwrite an edited transcript
        # with an unedited one depending on which finished last.
        self._queued = set()
        self._qlock = threading.Lock()
        self._asr = None
        self._align = None
        self._diar = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, clip):
        """Queue a clip. Returns False if it is already queued or running."""
        with self._qlock:
            if clip in self._queued or self.busy == clip:
                return False
            self._queued.add(clip)
        self.q.put(clip)
        return True

    def is_pending(self, clip):
        with self._qlock:
            return clip in self._queued or self.busy == clip

    def _load(self):
        # The models are loaded once and kept.
        #
        # This used to rebuild them whenever the custom word list changed, on
        # the reasoning that boosting is baked in at load time. It is not:
        # decode-time boosting was tried, measured, and removed -- see the
        # note below -- and the word list is applied afterwards in
        # apply_vocabulary(). So every edit to the list cost a twenty-second
        # reload that changed nothing about the result, and did it by
        # assigning a new model over a reference the old one was still held
        # by, so both sat in GPU memory at once. Three models, one card, and
        # no reason for the risk.
        if self._asr is not None:
            return
        self.notify("log",
                    text="loading transcription models (first run, ~20s)")
        import whisperx
        # Decode-time boosting is deliberately NOT used. Measured on a real
        # 45-second recording: plain decoding produced two segments covering
        # the whole clip, while hotwords (with or without an initial_prompt)
        # produced one and silently dropped sixteen seconds of speech.
        # Conditioning the decoder on a bare word list makes it treat some
        # chunks as containing nothing.
        #
        # Custom words are applied afterwards instead, in apply_vocabulary().
        # That fixes the same mistakes -- including terms split across words --
        # and cannot cause audio to go missing.
        self._asr = whisperx.load_model("large-v3", "cuda",
                                        compute_type="float16", language="en")
        self._align = whisperx.load_align_model(language_code="en", device="cuda")
        token = os.environ.get("HF_TOKEN")
        if token:
            try:
                self._diar = whisperx.diarize.DiarizationPipeline(
                    model_name="pyannote/speaker-diarization-3.1",
                    token=token, device="cuda")
            except Exception as e:
                self.notify("log", text=f"diarization unavailable: {str(e)[:80]}")
        else:
            # Say so. Without a token every transcript comes out with nobody
            # in it, which reads as a diarization that found one speaker
            # rather than as a feature that never ran.
            self.notify("log", text="NO HF_TOKEN — transcribing without "
                                    "speaker labels; nobody can be named")
        self.notify("log", text="models ready")

    def _run(self):
        while True:
            clip = self.q.get()
            with self._qlock:
                self._queued.discard(clip)
            try:
                self.busy = clip
                self.notify("job", clip=clip, status="running")
                self._process(clip)
                self.notify("job", clip=clip, status="done")
            except Exception as e:
                self.notify("job", clip=clip, status="error", error=str(e)[:200])
                self.notify("log", text=f"transcription failed: {str(e)[:120]}")
            finally:
                self.busy = None

    def _process(self, clip):
        import whisperx
        self._load()
        path = os.path.join(DATA, clip)
        audio = whisperx.load_audio(path)

        res = self._asr.transcribe(audio, batch_size=16)
        model_a, meta = self._align
        res = whisperx.align(res["segments"], model_a, meta, audio, "cuda")

        names, embeddings = {}, {}
        if self._diar is not None:
            df, emb = self._diar(audio, return_embeddings=True)
            res = whisperx.assign_word_speakers(df, res)
            # pyannote returns a NaN embedding when a speaker cluster has too
            # little audio to compute a standard deviation over. Storing one
            # produces a transcript that cannot be encoded as valid JSON, so
            # the clip becomes unreadable and looks untranscribed forever --
            # and re-transcribing reproduces it. A NaN voiceprint could never
            # match anything anyway, so drop it and keep the transcript.
            clean = {}
            for k, v in (emb or {}).items():
                arr = np.asarray(v, dtype=np.float64).ravel()
                if arr.size and np.all(np.isfinite(arr)):
                    clean[k] = arr
            embeddings = {k: v.tolist() for k, v in clean.items()}
            names = identify(clean, clip=clip)

        terms = load_vocabulary()
        segs = [{"start": round(float(s["start"]), 2),
                 "end": round(float(s["end"]), 2),
                 "speaker": s.get("speaker"),
                 "text": apply_vocabulary(s["text"].strip(), terms)}
                for s in res["segments"] if s.get("text", "").strip()]

        # Drop any segment whose timing did not survive alignment, for the
        # same reason: one non-finite value makes the whole file unservable.
        segs = [x for x in segs
                if all(isinstance(x[k], (int, float)) and math.isfinite(x[k])
                       for k in ("start", "end"))]

        # Whisper writes a phrase over silence, and the diarizer is the check.
        #
        # Nine clips in the archive had a transcript of exactly one segment
        # reading "Thank you." or "Hmm." with no speaker on it. Measured, they
        # are room tone -- around -30 dBFS RMS across thirty seconds -- and
        # WhisperX's own VAD had already logged "No active speech found in
        # audio" for each. The phrase is the model's habit on near-silence,
        # and it was reaching search, the conversation view and the agent as
        # though somebody had said it.
        #
        # The test is the diarizer rather than a list of known phrases: if it
        # found no voice anywhere in the clip, no one spoke, whatever the
        # decoder wrote. A real short utterance -- "Yeah." -- is still kept,
        # because it comes with a speaker. Only applied when diarization
        # actually ran, so disabling it does not silently discard everything.
        if (self._diar is not None and segs
                and not any(x.get("speaker") for x in segs)
                and sum(len(x["text"]) for x in segs) <= HALLUCINATION_CHARS):
            self.notify("log", text=(
                f"{clip}: no voice found; discarding "
                f"{' '.join(x['text'] for x in segs)[:40]!r} as silence"))
            segs = []

        os.makedirs(TRANSCRIPTS, exist_ok=True)
        atomicio.write_json(transcript_path(clip),
                            {"clip": clip, "created": time.time(),
                             "segments": segs, "speakers": names,
                             "embeddings": embeddings},
                            indent=2, allow_nan=False)

        try:
            import index_db
            index_db.upsert_clip(clip)
        except Exception:
            pass

        # Embed the lines for meaning-based search. Best effort: if Ollama is
        # not up, keyword search still works and this clip is picked up by the
        # next rebuild rather than failing the transcription.
        try:
            import semantic
            r = semantic.index_clip(clip, segs)
            if r.get("failed"):
                self.notify("log", text=(
                    f"semantic index: {r['failed']} line(s) not embedded"
                    + (f" ({r['error']})" if r.get("error") else "")))
        except Exception as e:
            self.notify("log", text=f"semantic index skipped: {str(e)[:80]}")

        try:
            self.on_transcript(clip, segs, names)
        except Exception as e:
            self.notify("log", text=f"agent intake failed: {str(e)[:100]}")

    # ---------------------------------------------------------- consolidation

    # How much audio goes to the diarizer at once. The point of this pass is to
    # give it minutes instead of thirty seconds, but a sitting can run to five
    # hours and that should not be one call -- clustering cost is not linear in
    # the number of turns, and the whole window sits in GPU memory.
    WINDOW_SECONDS = 1200.0
    # Two windows of the same sitting are diarized independently, so SPEAKER_00
    # in one is unrelated to SPEAKER_00 in the next. They are linked afterwards
    # by comparing the pooled voiceprints, which is the same top-1 comparison
    # the store uses and is reliable at this range -- the windows are minutes
    # apart, not days.
    LINK_MIN = 0.75

    # A turn shorter than this makes an unreliable voiceprint, so it cannot
    # say anything useful about whether a slot holds one person or two.
    MIN_TURN_SECONDS = 1.5
    # Turns per slot to test. Purity is a spread measurement; a dozen samples
    # describe the spread as well as fifty and cost a quarter as much.
    PURITY_TURNS = 12

    def _embed_spans(self, audio, spans, sr=16000):
        """Embed individual stretches of audio with the diarizer's own model.

        Reaches into the pyannote pipeline for the embedding model it already
        has loaded rather than loading a second one -- a different embedder
        would answer a different question, and the question here is precisely
        "does the model that drew this boundary still think it was right".
        """
        import torch
        model = getattr(getattr(self._diar, "model", None), "_embedding", None)
        if model is None:
            return None
        out = []
        for a, b in spans:
            piece = audio[int(a * sr):int(b * sr)]
            if len(piece) < int(self.MIN_TURN_SECONDS * sr):
                out.append(None)
                continue
            try:
                w = torch.from_numpy(np.asarray(piece, dtype=np.float32))
                w = w.reshape(1, 1, -1)
                dev = getattr(model, "device", None)
                if dev is not None:
                    w = w.to(dev)
                v = np.asarray(model(w)).ravel()
                out.append(unit(v) if np.all(np.isfinite(v)) and v.size else None)
            except Exception:
                out.append(None)
        return out

    # Splitting parameters, derived from what the archive actually measured
    # rather than picked: same-person turns under held conditions sit at a
    # median of 0.88 with a 10th percentile of 0.786, and different people at a
    # median of 0.099 with a 90th percentile of 0.277. So a genuine two-person
    # split should show each group holding together near 0.8 and the two groups
    # sitting apart down near 0.1. The bars below are slack against those
    # numbers on purpose -- a split that only just clears them is one this
    # cannot tell from noise, and refusing it is the safe answer.
    SPLIT_MIN_TURNS = 6
    SPLIT_TURNS_TESTED = 20
    SPLIT_WITHIN_MIN = 0.70
    SPLIT_BETWEEN_MAX = 0.45

    def _split_slot(self, vecs):
        """Try to separate a slot's turns into two voices.

        Flagging an impure slot protects the store but throws the speech away.
        Most flagged slots are not noise, they are two people the diarizer ran
        together -- and two people can be separated, which recovers the audio
        instead of discarding it.

        It is also a diagnostic, though a narrower one than it first looked.
        A slot that falls into two tight groups was two people. A slot that
        refuses is NOT thereby shown to be bad audio -- measured on this
        archive, the slots that refuse have groups that sit far apart
        (between ~0.06, where different people sit) while neither group holds
        together (within ~0.3, against 0.79 for genuine same-person turns), and
        no number of clusters from two to six fixes that. Their turns are
        mutually unlike at every granularity, which two speakers would not
        produce and three would not either.

        What those slots have in common is content, not level: television and
        video with several characters, music and effects under the speech. A
        single-narrator video scores 0.96 on this same test. So a refusal means
        "this slot is not two voices" and nothing more; what it actually is
        remains open, and the honest response is to keep it out of the store
        rather than to explain it.

        Medoids, not centroids: the representative of a group is an actual turn
        from it, for the same reason the store keeps references individually.
        Averaging two voices is what created this problem one level up.

        Always returns a dict. `split` says whether it worked, and the rest is
        the evidence either way -- a refused split is a finding, not a failure,
        so its numbers are reported rather than dropped.
        """
        n = len(vecs)
        if n < self.SPLIT_MIN_TURNS:
            return {"split": False, "reason": "too few usable turns",
                    "turns": n}
        M = np.stack(vecs)
        S = M @ M.T

        # Seed on the two least-alike turns: if the slot holds two people, the
        # furthest-apart pair is one from each.
        iu = np.triu_indices(n, k=1)
        flat = S[iu]
        a, b = iu[0][int(np.argmin(flat))], iu[1][int(np.argmin(flat))]

        labels = np.zeros(n, dtype=int)
        for _ in range(10):
            labels = (S[a] < S[b]).astype(int)
            labels[a], labels[b] = 0, 1
            groups = [np.where(labels == 0)[0], np.where(labels == 1)[0]]
            if min(len(g) for g in groups) < 2:
                return {"split": False, "reason": "one side collapsed"}
            # New medoid per group: the member most like the rest of its group.
            new = []
            for g in groups:
                new.append(int(g[np.argmax(S[np.ix_(g, g)].sum(axis=1))]))
            if [a, b] == new:
                break
            a, b = new

        groups = [np.where(labels == 0)[0], np.where(labels == 1)[0]]
        if min(len(g) for g in groups) < 2:
            return {"split": False, "reason": "one side collapsed"}
        within = []
        for g in groups:
            sub = S[np.ix_(g, g)]
            iu2 = np.triu_indices(len(g), k=1)
            within.append(float(sub[iu2].mean()) if len(g) > 1 else 1.0)
        between = float(S[np.ix_(groups[0], groups[1])].mean())

        quality = {"within": [round(w, 3) for w in within],
                   "between": round(between, 3),
                   "sizes": [int(len(g)) for g in groups]}
        if min(within) < self.SPLIT_WITHIN_MIN or between > self.SPLIT_BETWEEN_MAX:
            # Not two voices. On this archive that most likely means the audio
            # is too poor to voiceprint a turn at a time -- which is worth
            # knowing, and is not something a better diarizer would fix.
            quality.update(split=False, reason="no clean separation")
            return quality
        quality.update(split=True, labels=labels.tolist(), medoids=[int(a), int(b)])
        return quality

    def _slot_purity(self, audio, slot_turns):
        """Does a slot's own speech agree with itself?

        A slot is a claim that everything in it is one person. When the
        diarizer is wrong about that, nothing downstream can tell: one name
        goes onto two people's speech, and the reference stored from it is a
        blend of two voices that looks perfectly ordinary in vector space --
        indistinguishable from a real voiceprint, and quietly wrong forever.
        Distance cannot find it later, so it has to be caught here.

        The test is the slot against itself. Embed its turns separately and
        look at the spread: one person's turns agree closely, two people's
        turns fall into two groups and drag the low end down. Reported, not
        enforced -- a low score is a reason for a human to listen, not for the
        machine to discard somebody's speech.

        Returns the pairwise scores too. Turns inside one slot are the same
        person under identical conditions, and turns in two different slots of
        the same window are different people under identical conditions --
        which is the matched-condition comparison every threshold in
        speaker_store is currently guessing without.
        """
        report, vectors, tested = {}, {}, {}
        for slot, turns in slot_turns.items():
            longest = sorted(turns, key=lambda t: t[1] - t[0],
                             reverse=True)[:self.SPLIT_TURNS_TESTED]
            got = self._embed_spans(audio, longest) or []
            vecs, spans = [], []
            for span, v in zip(longest, got):
                if v is not None:
                    vecs.append(v)
                    spans.append(span)
            vectors[slot] = vecs
            tested[slot] = spans
            if len(vecs) < 3:
                report[slot] = {"turns_tested": len(vecs), "coherence": None,
                                "worst_pair": None, "suspect": False}
                continue
            M = np.stack(vecs)
            sims = M @ M.T
            iu = np.triu_indices(len(vecs), k=1)
            pairs = sims[iu]
            coherence = float(np.mean(pairs))
            worst = float(np.min(pairs))
            report[slot] = {
                "turns_tested": len(vecs),
                "coherence": round(coherence, 3),
                "worst_pair": round(worst, 3),
                # Two people merged shows up as a group of pairs that disagree,
                # not one odd turn -- so the bottom decile, not the minimum.
                "p10": round(float(np.percentile(pairs, 10)), 3),
                "suspect": bool(np.percentile(pairs, 10) < 0.55),
            }
        return report, vectors, tested

    def consolidate(self, clips):
        """Re-diarize a finished conversation as one stretch of audio.

        Transcription runs per clip, because a clip arrives every thirty
        seconds and waiting for silence to transcribe would make the whole
        interface lag behind the room. Diarization has no such excuse and
        every reason not to: a thirty-second window gives the diarizer thirty
        seconds to tell two voices apart, produces a voiceprint pooled over
        whatever fragment of that a person spoke, and labels them SPEAKER_00
        with no relation to the SPEAKER_00 in the clip before. Twenty minutes
        of the same conversation gives it the whole exchange, one voiceprint
        per person pooled over every second they spoke, and labels that hold
        across all of it.

        So the per-clip labels stay as a provisional answer that arrives
        immediately, and this overwrites them with a better one once the
        conversation is over. Transcripts stay one JSON per clip -- the index,
        search and the whole UI are built on that and there is no reason to
        move it.
        """
        import whisperx
        self._load()
        if self._diar is None:
            return {"ok": False, "error": "diarization is not available"}

        # Load once, in order, remembering where each clip lands on the
        # sitting's clock so turns can be handed back to the right clip.
        loaded, offset = [], 0.0
        for clip in clips:
            path = os.path.join(DATA, clip)
            if not os.path.exists(path) or not os.path.exists(transcript_path(clip)):
                continue
            try:
                audio = whisperx.load_audio(path)
            except Exception:
                continue
            dur = len(audio) / 16000.0
            loaded.append({"clip": clip, "audio": audio,
                           "start": offset, "end": offset + dur})
            offset += dur
        if not loaded:
            return {"ok": False, "error": "no readable audio in this conversation"}

        windows, cur = [], []
        for item in loaded:
            if cur and item["end"] - cur[0]["start"] > self.WINDOW_SECONDS:
                windows.append(cur)
                cur = []
            cur.append(item)
        if cur:
            windows.append(cur)

        # Diarize each window; collect its slots with their pooled voiceprints.
        slots, turns = [], []
        slot_quality, calibration = {}, {"same": [], "different": []}
        # Voiceprints for slots this pass separated. pyannote pooled one vector
        # for the merged slot, and that vector is a blend of two people -- so
        # each side gets its medoid turn instead, an actual recording of that
        # voice rather than an average of two.
        split_vecs = {}
        for wi, win in enumerate(windows):
            audio = np.concatenate([x["audio"] for x in win])
            base = win[0]["start"]
            self.notify("log", text=(
                f"diarizing {len(win)} clip(s), "
                f"{(win[-1]['end'] - base) / 60:.0f} min, as one pass"))
            df, emb = self._diar(audio, return_embeddings=True)
            local = {}
            for k, v in (emb or {}).items():
                arr = np.asarray(v, dtype=np.float64).ravel()
                # Same NaN guard as the per-clip path: pyannote returns a
                # non-finite embedding for a cluster with too little audio,
                # and one stored makes the transcript unencodable.
                if arr.size and np.all(np.isfinite(arr)):
                    local[k] = arr
            local_turns = {}
            for row in df.itertuples():
                spk = getattr(row, "speaker", None)
                if spk is None:
                    continue
                local_turns.setdefault(spk, []).append(
                    (float(row.start), float(row.end)))

            # Ask the diarizer whether it still believes its own boundaries,
            # while this window's audio is still in hand.
            purity, turn_vecs, tested_spans = self._slot_purity(audio, local_turns)

            # A flagged slot is usually two people, not noise. Try to separate
            # them before writing anything off: a clean split recovers the
            # speech instead of discarding it, and a refusal is itself the
            # answer to why the slot was flagged.
            for spk, r in list(purity.items()):
                if not r.get("suspect"):
                    continue
                sq = self._split_slot(turn_vecs.get(spk) or [])
                r["split_attempt"] = {k: v for k, v in sq.items()
                                      if k not in ("labels", "medoids")}
                if not sq.get("split"):
                    continue
                labels = sq["labels"]
                spans = tested_spans.get(spk) or []
                # Hand every turn in the slot to a side. Turns long enough to
                # embed go by their own vector; the short ones -- which cannot
                # be voiceprinted and are what made this hard -- go with the
                # embedded turn they sit closest to in time.
                anchors = [(0.5 * (a + b), labels[i])
                           for i, (a, b) in enumerate(spans) if i < len(labels)]
                sides = {0: [], 1: []}
                for a, b in local_turns[spk]:
                    mid = 0.5 * (a + b)
                    side = min(anchors, key=lambda x: abs(x[0] - mid))[1]
                    sides[side].append((a, b))
                if not (sides[0] and sides[1]):
                    continue
                originals = turn_vecs[spk]
                del local_turns[spk]
                del purity[spk]
                del turn_vecs[spk]
                for side, spans_out in sides.items():
                    sub = f"{spk}#{'ab'[side]}"
                    idx = [i for i, l in enumerate(labels) if l == side]
                    local_turns[sub] = spans_out
                    turn_vecs[sub] = [originals[i] for i in idx]
                    split_vecs[(wi, sub)] = originals[sq["medoids"][side]]
                    purity[sub] = {"turns_tested": len(idx),
                                   "coherence": sq["within"][side],
                                   "worst_pair": None,
                                   "p10": sq["within"][side],
                                   "suspect": False, "from_split": spk}
                self.notify("log", text=(
                    f"slot {spk} separated into two voices "
                    f"(within {sq['within']}, between {sq['between']})"))
            for spk, r in purity.items():
                slot_quality[(wi, spk)] = r
                if r.get("suspect"):
                    self.notify("log", text=(
                        f"slot {spk} in window {wi + 1} disagrees with itself "
                        f"(p10 {r['p10']}); it may be two people"))
            # A slot that disagrees with itself cannot supply same-person
            # pairs -- that is exactly how a merged slot would teach the
            # calibration that two different people score alike, and poison
            # the measurement it exists to provide. Its turns still serve as
            # different-person evidence against other slots.
            _collect_calibration(
                calibration, turn_vecs,
                trusted={k for k, v in purity.items() if not v.get("suspect")})
            # Turns and slots are both built from local_turns rather than
            # from the diarizer's frame, because splitting rewrites it. A slot
            # that was separated uses its medoid; one left alone keeps
            # pyannote's pooled vector, which for a coherent slot is pooled
            # over every second that voice spoke and is the better reference.
            for spk, spans_out in local_turns.items():
                for a, b in spans_out:
                    turns.append({"start": base + a, "end": base + b,
                                  "local": (wi, spk)})
                vec = split_vecs.get((wi, spk))
                if vec is None:
                    vec = local.get(spk)
                if vec is None or not np.all(np.isfinite(np.asarray(vec))):
                    continue
                slots.append({"key": (wi, spk), "vec": unit(vec),
                              "final": None})

        if not slots:
            return {"ok": False, "error": "no voices found"}

        # Link slots across windows. Same person, same conversation, minutes
        # apart -- the regime the embedder is strongest in.
        next_label = 0
        for s in slots:
            # Link to the closest already-labelled slot that clears the bar,
            # not merely the first one found -- with several speakers in a
            # conversation, first-past-the-post attaches a voice to whichever
            # slot happened to be earlier in the list.
            best_other, best_score = None, self.LINK_MIN
            for other in slots:
                if other is s or other["final"] is None:
                    continue
                sc = float(s["vec"] @ other["vec"])
                if sc >= best_score:
                    best_other, best_score = other, sc
            if best_other is not None:
                s["final"] = best_other["final"]
            else:
                s["final"] = f"SPEAKER_{next_label:02d}"
                next_label += 1
        by_key = {s["key"]: s["final"] for s in slots}

        # One pooled voiceprint per final speaker: the longest-observed slot
        # standing for it, rather than a mean of the slots, for the same
        # reason the store keeps references individually.
        seconds = {}
        for t in turns:
            f = by_key.get(t["local"])
            if f:
                seconds[f] = seconds.get(f, 0.0) + (t["end"] - t["start"])
        best = {}
        for s in slots:
            f = s["final"]
            if f not in best:
                best[f] = s["vec"]
        embeddings = {f: v.tolist() for f, v in best.items()}

        # Purity was measured per window slot; report it against the final
        # speaker each one was linked to, keeping the least flattering result
        # so a merged slot is not hidden by a clean one it was joined with.
        final_quality = {}
        for key, r in slot_quality.items():
            f = by_key.get(key)
            if not f:
                continue
            cur = final_quality.get(f)
            if cur is None or (r.get("p10") is not None and
                               (cur.get("p10") is None or r["p10"] < cur["p10"])):
                final_quality[f] = r

        # Hand the turns back to the clips they came from, by overlap.
        # One stamp for the whole pass: it is what marks these clips as having
        # been diarized together, and time.time() per clip made every clip look
        # like a separate conversation.
        stamp = time.time()
        changed = 0
        for item in loaded:
            tp = transcript_path(item["clip"])
            try:
                t = json.load(open(tp))
            except Exception:
                continue
            if t.get("edited"):
                continue          # a name set by hand outranks any guess
            for seg in (t.get("segments") or []):
                try:
                    a = item["start"] + float(seg["start"])
                    b = item["start"] + float(seg["end"])
                except (TypeError, ValueError, KeyError):
                    continue
                best_spk, best_ov = None, 0.0
                for turn in turns:
                    ov = min(b, turn["end"]) - max(a, turn["start"])
                    if ov > best_ov:
                        best_ov, best_spk = ov, by_key.get(turn["local"])
                if best_spk:
                    seg["speaker"] = best_spk
            t["embeddings"] = embeddings
            t["speakers"] = identify({k: np.asarray(v) for k, v in embeddings.items()},
                                     clip=item["clip"])
            t["slot_quality"] = final_quality
            # A voice the diarizer cannot keep straight must not become
            # somebody's stored reference: a blend of two people looks
            # perfectly ordinary in vector space and nothing downstream can
            # ever tell. Naming it by hand is still allowed -- a person
            # listening can hear what the model cannot -- but it is refused
            # for automatic enrolment and flagged in the interface.
            for spk, q in final_quality.items():
                if q.get("suspect") and spk in (t.get("speakers") or {}):
                    t["speakers"][spk]["impure"] = True
                    t["speakers"][spk]["name"] = None
            t["consolidated"] = stamp
            atomicio.write_json(tp, t, indent=2, allow_nan=False)
            changed += 1

        _save_calibration(calibration)
        return {"ok": True, "clips": changed, "windows": len(windows),
                "voices": len(embeddings),
                "seconds": {k: round(v, 1) for k, v in seconds.items()},
                "slot_quality": final_quality,
                "suspect_slots": [k for k, v in final_quality.items()
                                  if v.get("suspect")]}
