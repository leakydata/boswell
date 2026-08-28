# Speaker identity: what is wrong and what to build instead

Written 2026-08-28 from measurements taken on the `boswell-gemma4` archive.
This is a design brief for a rewrite of the speaker identification path. It is
self-contained: a session starting here does not need the conversation it came
from.

**Scope.** Only the identity path. Capture, store-and-forward, transcription,
the index and the web UI are not in question and should not be rewritten --
most of the long comments in those files are bugs that cost real time to find,
and throwing them out would re-earn them.

---

## 1. Where the measurements come from

Everything below was measured on `../boswell-gemma4/data`, because that is the
larger archive: 6520 clips over 4.06 continuous days, all at 16 kHz, 1379 voice
embeddings, 256 dimensions, from `pyannote/speaker-diarization-community-1`.

**This installation reproduces the central finding independently.** It holds
1831 transcripts, 1629 clips (117 still at 8 kHz), and five enrolled people
with eleven samples between them -- a different archive, 679 voices over 16
sittings and 2.5 days. Running `tools_speaker_diag/nn_time.py` here:

- **33.3% of nearest neighbours fall within two minutes**, against 0.7% by
  chance -- a 48x enrichment
- nearest neighbours land in a **different sitting 6.2%** of the time where
  chance gives 56.8% -- suppressed 9x below random, a larger effect than the
  fork shows
- of the 362 voices whose best match clears 0.85, only **1.7% are cross-sitting**

So §3.1 is not an artifact of one archive. The remaining figures in §3 --
the controls and the ground-truth comparison in §3.3 and §3.4 -- were computed
on the fork only and should be re-run here.

This installation is also still running `MATCH_THRESHOLD = 0.60` in
`web/pipeline.py`, the value found to be wrong in §2, and has no
`check_speakers.py`, so nothing here has ever measured whether its names are
right. That is item 2 of §6 and it is the most urgent thing in this document.

---

## 2. What was already known

From `../boswell-gemma4/KNOWN_ISSUES.md`, established before this work:

- A YouTube narrator discussing sapphire and 355 nm lasers was filed under
  Nathan's name for weeks, at threshold 0.60.
- The measurement that had reassured us was circular: enrolment samples scored
  against the centroids they were themselves averaged into. They score high by
  construction.
- Held out properly -- 980 embeddings never used to build a centroid -- the
  score distribution runs **continuously from 0.85 down through 0.74**. The
  largest break anywhere in 0.68--0.92 is **0.009**. There is no boundary to
  discover, so any threshold is a judgement about which error to make.
- At 0.60 the archive named 209 of 985 embeddings. At 0.85 it names 22.

What that did not explain is *why* there is no boundary. That is §3.

---

## 3. What the new measurements found

Three tests, all read-only, all reproducible from `tools_speaker_diag/`.

### 3.1 Nearest neighbours pile up in time (`nn_time.py`)

For each of 884 voices with at least 3 s of speech, find its single nearest
neighbour among all the others and look only at *when* that neighbour was
recorded. 874 of the 884 were built from 25 s or more of speech, so these are
best-case voiceprints, not scraps.

| time gap to nearest neighbour | observed | by chance |
|---|--:|--:|
| < 1 min | 25.1% | 0.2% |
| 1--2 min | 16.0% | 0.3% |
| 2--10 min | 34.5% | 2.0% |
| 1--6 hours | 5.9% | 20.1% |
| > 6 hours | 8.8% | 70.3% |

41% of nearest neighbours are within two minutes against 0.5% by chance -- an
80x enrichment. Nearest neighbours land in a different sitting 13.9% of the
time where chance alone gives 79.4%, so cross-sitting matching is suppressed
nearly 6x *below* random. Of the 593 voices whose best match clears 0.85, **97%
are from the same sitting, median gap 2.0 minutes.**

### 3.2 Tight clusters are sittings, not people (`cluster_diag.py`)

Agglomerative clustering on cosine distance. At radius 0.90, sixteen clusters
reach five members and **not one spans more than a single sitting**. The
largest are 8 to 36 minutes long. At 0.85 the only cluster crossing a sitting
boundary is the one containing Nathan, and it is the eighth largest.

### 3.3 The controls, which stopped this being a simpler story (`controls.py`)

The above has an innocent explanation: nearby clips may just contain the same
person and distant clips different people, in which case the embeddings are
working correctly. Two controls hold conditions fixed instead of varying them.

**Two different speakers inside one 30-second clip** -- identical room, mic,
gain, codec, same instant, only the person differs. They score **0.650 mean,
none above 0.85**. If the embedding were mostly describing the recording, two
people sharing one recording would score high. They do not. **So "the embedder
encodes the room, not the person" is too strong.** Caveat: only 3 such pairs
exist in the archive. A hint, not proof.

**Decay inside one sitting**, room held constant:

| gap within one sitting | median | p90 | >= 0.85 |
|---|--:|--:|--:|
| < 1 min | 0.851 | 0.936 | 50.6% |
| 5--10 min | 0.494 | 0.891 | 20.0% |
| 10--30 min | 0.203 | 0.788 | 5.0% |
| 30--120 min | 0.176 | 0.450 | 0.5% |

Real decay, but confounded: over two hours in one room the speaker almost
certainly changed. This cannot separate drift from speaker turnover and should
not be leaned on.

### 3.4 The only ground truth in the archive

Put side by side, the two labelled quantities are the cleanest comparison
available:

- **Same person** -- Nathan, 5 enrolment samples, 6 minutes apart, one room:
  **0.815**
- **Different people** -- same clip, same instant: **0.650**

Identity signal exists. But the gap is about **0.16**, and within-person
variation over a few minutes is nearly as large as the between-person
separation. That is a weak signal, not an absent one.

It also relocates the bug. **0.85 sits above where the same person lands.**
Nathan measured against Nathan, best case, is 0.815. The threshold was raised
to stop false names and is high enough to reject true ones, which is why it
names 22 of 985.

### 3.5 The enrolment sets have no cross-condition coverage

Every sample for both enrolled people came from a single sitting. Nathan's five
span six minutes. NileRed's three span one sitting. There is no evidence in the
store about how anybody sounds on a different day.

**And the code prevents acquiring any.** `save_speaker()` in
`web/pipeline.py` rejects a sample that does not resemble the stored reference:

```python
if sim < OUTLIER_MIN:          # 0.55
    return {"ok": False, "reason": "outlier",
            "detail": f"this sounds unlike the {name} already enrolled ..."}
```

A sample from a different room is refused as "may be a misattributed line."
The gate was written for a centroid store, where a dissimilar sample poisons
the mean. It is exactly backwards for what §5 proposes.

---

## 4. The conclusion in one paragraph

The embedder does separate people under matched conditions, but its margin is
narrow -- roughly 0.16 between same-person and different-person -- and
within-person variation across time and setting is comparable to it. Averaging
a person into one centroid destroys the little coverage there is; a threshold
picked to suppress false names then also rejects true ones. The fix is not a
better threshold and not a bigger database. It is to (a) do identity work where
the embedder is demonstrably strong, which is within a sitting, and (b) store
one reference per condition instead of one average per person, so drift is
covered rather than averaged away.

---

## 5. The design

### 5.1 The processing unit is the sitting, not the clip

A 30-second clip is a BLE transport artifact. Today `_diarize(path, audio)`
runs on one WAV at a time, so a 10-minute stretch produces ~20 unrelated sets
of `SPEAKER_00`/`SPEAKER_01` labels and 20 separate 30-second voiceprints, with
nothing connecting them.

A **sitting** is a continuous run of clips with no gap longer than ~15 minutes.
In the measured archive that gives 37 sittings over 4 days: median 9.5 minutes,
13 under 5 minutes, 6 over 30 minutes, longest 324 minutes.

The boundary already exists in the system -- it is the 90-seconds-of-silence
trigger that closes a conversation for the extraction pass. Reuse it.

**Cap the window.** The 324-minute sitting should not go to pyannote in one
call. Process in windows of roughly 20--30 minutes with a little overlap so
speakers link across the boundary. The exact number is worth measuring; the
point is only that the unit is minutes, not 30-second frames.

### 5.2 Diarize the whole sitting in one pass

pyannote clusters internally across the full span and returns consistent labels
for all of it, plus one embedding per speaker pooled over every second they
spoke. This uses the embedder only in the regime §3.1 shows it is strong in.

Cost is small: 1.05 s per 30-second clip on CUDA, so a 20-minute window is well
under a minute.

**This change is worth making on its own**, independently of everything below.
It makes every voiceprint dramatically better by pooling minutes instead of
seconds, and it collapses the number of identity decisions from 1379 across the
archive to under 150.

### 5.3 A slot is the unit of identity

The diarizer produces exactly two things:

```python
turns      = [{"speaker": "SPEAKER_00", "start": 4.2, "end": 9.8}, ...]
embeddings = {"SPEAKER_00": <256 floats>, ...}
```

One **slot** = one distinct voice in one processed window. One embedding, one
identity decision, applied to every turn in that slot.

Transcription stays independent -- it can run per clip in parallel, or over the
whole sitting if you want sentences that are not cut at clip boundaries.
Speaker labels attach afterwards by timestamp overlap. Timestamps do the
alignment job correctly *within* a sitting; they cannot do the linking job
across sittings, which is why §5.2 matters.

**Slot quality is now the ceiling on label accuracy.** If the diarizer merges
two people into one slot, one name goes onto both people's speech and the
stored reference is a blend of two voices, which poisons future matching and
looks perfectly normal in vector space. Guard it: embed each turn in a slot
separately and check they agree with one another. A slot whose own turns
disagree is probably two people and should be flagged rather than named.

### 5.4 The store: one row per reference, never a centroid

```
references(id, person, vector, source_sitting, seconds, origin, created)
   origin ∈ {manual, confirmed, auto}
```

- **No centroid, ever.** Match a new slot against every stored reference and
  take the best.
- **Add only on a miss.** Store a new reference only when every existing one
  failed to match. Each stored vector is then by construction a condition not
  previously covered. This is also the deduplication: there is nothing
  redundant to consolidate later, so no periodic radius clustering is needed.
- **Invert the outlier gate.** A dissimilar sample is the valuable one. Keep a
  floor only to catch obvious garbage, not to enforce resemblance.
- **Every reference individually deletable**, with provenance, so a mislabelled
  one can be pulled without rebuilding anything.
- **Track which reference won each match.** Store the winning reference id with
  the score.

Size is not a constraint: the archive accumulates ~135k vectors a year in
total, labelled references are a small fraction of that, and brute-force
matching over 135k 256-d vectors is about 10 ms. A vector index is not needed
for speed. It is worth adopting only to get one transactional store -- the
open `KNOWN_ISSUES` item about samples, metadata and centroids being three
files that can disagree after a crash -- and to make archive-wide voice search
possible.

### 5.5 Accept by threshold *and* margin

§2 established there is no absolute cut to find. A margin does not need one:
`Nathan 0.86, Ron 0.85` should be unknown; `Nathan 0.86, next-best 0.61` should
not. Use best-minus-runner-up, with a low absolute floor only to reject silence
and noise.

**Do not carry 0.85 forward.** It was derived against centroids. Under top-1
over individual references every score rises, because the nearest single
reference almost always beats the mean of all of them, so 0.85 would suddenly
name a large slice of what it currently rejects -- and it would look like the
new design working brilliantly on day one. Re-derive it on held-out data for
the new scoring rule. `check_speakers.py` in `../boswell-gemma4` has the right
methodology and should be ported here.

### 5.6 Unmatched slots are a first-class result

Leaving a voice unnamed is a correct answer. Make it productive:

- Cluster unmatched slot embeddings against each other so a recurring stranger
  becomes "unknown A -- 14 minutes, 3 sittings" rather than dozens of
  unrelated `SPEAKER_00`s.
- Naming it once resolves every sitting in the chain. `_rematch_clips()`
  already does retroactive relabelling from stored embeddings without
  re-transcribing.

### 5.7 What a labelling record needs

Per slot, for the queue:

- the embedding (matching)
- total speech seconds (is there enough to trust, or to bother asking)
- the turn list (where this voice appears)
- **playable audio of just this voice** -- their turns concatenated. This does
  not exist today and is what makes manual labelling fast rather than a chore.
- the transcript text for their turns, joined by timestamp -- content often
  identifies someone faster than voice does
- **ranked candidates with scores, top 3** -- already computed inside
  `identify()` and currently thrown away. A visible near-miss is confirmable in
  one click, and every confirmation is a new reference.

### 5.8 Pruning by evidence, not geometry

A harmful reference looks perfectly normal in vector space, so distance cannot
find it. Use the match log instead:

- a reference that repeatedly wins matches you then correct is damaging --
  delete it
- a reference that never wins anything is dead weight and harmless -- leave it,
  it may be covering a condition not hit again yet, which is its purpose
- re-run the slot-purity check of §5.3 on stored references as more evidence
  accumulates

---

## 6. Build order

1. **Sitting-level diarization (§5.1--5.3).** Unconditionally right, helps
   whatever else turns out to be true, and is independent of the store.
2. **Port `check_speakers.py` and `tools_reidentify.py`** from
   `../boswell-gemma4`, then measure *this* archive. This installation is still
   at threshold 0.60 with no held-out measurement, which is the configuration
   that produced the misfiling described in §2.
3. **The store rewrite (§5.4--5.5)**: drop the centroid, invert the outlier
   gate, add-on-miss, threshold plus margin re-derived.
4. **The labelling queue (§5.6--5.7)**, including per-slot audio extraction.
5. **Pruning (§5.8)** once there is a match log worth mining.

---

## 7. Open questions, cheapest first

**Is cross-sitting identity recoverable at all?** Unmeasured, and it decides how
much of §5.4--5.5 is worth building. If drift is total, the honest system is
"consistent voices within a sitting, named by hand each time."

The test takes ten minutes and no code: read the same short paragraph four
times -- twice back to back, once an hour later in the same room, once tomorrow
somewhere else. Six pairs, perfect ground truth. If the back-to-back readings
score 0.95 and tomorrow's scores 0.6, drift is real and the capture path is
implicated. If all four sit near 0.8, there is no drift, the embedder simply
has a narrow margin, and the threshold is what was miscalibrated.

**Would a stronger embedder fix it?** Testable on existing audio with no
re-recording. `.venv` here has torch 2.8.0+cu128 with CUDA and pyannote 4.0.7.
Swap in an ECAPA-TDNN or TitaNet embedder, rerun `tools_speaker_diag/` over the
same WAVs. If a better model clusters by person on identical audio, it is the
model and the hardware is exonerated.

**Is the capture path damaging identity?** The clean-microphone control has
been on the open list in both projects and has never been recorded. Sharpened
protocol: same speaker, three or more rooms and mic positions, wearable and USB
mic capturing simultaneously, then run `nn_time.py` on both sets. If the
clean-mic vectors cluster by person across rooms while the wearable's cluster by
room, it is the capture path -- and 8 kHz-origin ADPCM through a PDM mic
becomes the thing to fix.

**Speaker accuracy on real multi-voice audio is still unmeasured** in both
projects. Both two-speaker benchmark cases were read solo. Nothing above
changes that; it needs a second person.

---

## 8. Reproducing the measurements

```
cd /home/scholyx/Documents/electronics/nRF52840
python3 tools_speaker_diag/cluster_diag.py     # sitting structure, clustering
python3 tools_speaker_diag/nn_time.py          # nearest-neighbour time gaps
python3 tools_speaker_diag/controls.py         # matched-condition controls
```

All three are read-only against `data/`. They default to this installation's
archive; set `BOSWELL_ROOT` to point them elsewhere, e.g.

```
BOSWELL_ROOT=../boswell-gemma4 python3 tools_speaker_diag/nn_time.py
```

They need numpy and scipy. The system python3 has both; this repo's `.venv`
does not have scipy.
