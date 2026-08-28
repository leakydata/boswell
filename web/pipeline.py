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
                 "matched": 0, "clustered": 0, "new_clusters": 0}
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


def labelling_queue(limit=50):
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
        for cl in sdb.unknown_clusters(c)[:limit]:
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
