# Web Interface Review

Scope: `web/**`, plus relevant host-side data files and protocol parsing used by the web service.

Validation run:

- `bash web/check_ui.sh` passed.
- `python3 -m py_compile web/*.py host/*.py` passed.

## Highest-Priority Issues

### 1. Conversation grouping still mixes timestamp models

File: `web/index_db.py:235-253`

`conversations()` sorts clips with `started_at(c)`, which uses device-time sidecars when available. Inside the grouping loop it recomputes `start` as `modified - seconds`, reintroducing file-mtime assumptions for recovered or variable-length clips.

Recommendation: use `started_at(c)` consistently for grouping and initialization. If `device_times()` exists, use the recorded end time for group end as well. Add a test with recovered clips whose mtime order differs from device-time order.

### 2. People tab has HTML injection risk

File: `web/static/index.html:953-962`

The People tab interpolates `p.name`, `p.count`, `e.clip`, `e.seconds`, `e.score`, and joined sample metadata into `innerHTML` without escaping. Other transcript paths mostly use `esc()`, so this is an inconsistent rendering boundary.

Recommendation: build People rows with DOM nodes and `textContent`, or apply `esc()` to every dynamic value. Treat local transcript/name data as untrusted text.

### 3. Semantic indexing failures are silent

File: `web/semantic.py:100-103`

When Ollama embedding fails, indexing silently skips the segment. Users get an incomplete semantic index with little diagnostic evidence.

Recommendation: return `added`, `failed`, and a bounded first error message from `index_clip()`. Show/log that in rebuild and transcription flows.

### 4. API body validation is ad hoc

Files: `web/server.py` endpoints including `/api/export`, `/api/conversation`, `/api/agent`, `/api/vocabulary`, `/api/label`, and deletion routes.

Many endpoints accept raw `dict` and manually check only the fields needed at the moment. This works, but makes it easier for bad types, oversized lists, or malformed clip names to leak into later code.

Recommendation: add small Pydantic request models for mutating endpoints. Keep the models narrow and practical: clip names list, export format enum, label request, agent config, vocabulary terms.

## Reliability Recommendations

### Use atomic writes for user data

Files include:

- `web/pipeline.py:61-72`, `453-456`
- `web/server.py:912`, `1200`, `1223`, `1411`
- `web/agent_runner.py:265-297`

Transcripts, speaker metadata, vocabulary, and agent JSONL rewrites are written directly to final paths. A crash or power loss can leave truncated JSON/NPZ.

Recommendation: write to a temp file in the same directory, flush/fsync, then `os.replace()`. For append-only JSONL, consider file locking if the CLI agent and web agent may run together.

### Normalize BLE frame parsing for web and host tools

Files:

- `web/server.py`
- `host/ble_capture.py`
- `host/tune_gain.py`
- `host/test_storeforward.py`

Several places parse the same BLE frame format manually. They share the ADPCM decoder, but not header parsing or frame semantics.

Recommendation: introduce a shared parser returning seq, flags, predictor, index, sample count, timestamp, source flags, payload, and decoded PCM. This reduces protocol drift as Zephyr evolves.

### Improve queue feedback for conversation transcription

File: `web/static/index.html:1739-1746`

The conversation "Transcribe missing" button loops over clips and ignores per-clip failures. The backend protects edited transcripts with a 409 unless `force=true`; the UI can still say "queued" even if some requests were refused.

Recommendation: return/display queued, skipped, and refused counts. Preserve edited-transcript protection in the UI instead of hiding it.

### Warn loudly when open network binding has no token

File: `web/server.py:1530-1532`

The server binds to `0.0.0.0` and allows unauthenticated use when `BOSWELL_TOKEN` is unset. The README warns about this, but runtime should also warn.

Recommendation: if host is not loopback and `BOSWELL_TOKEN` is empty, print a prominent startup warning. Consider defaulting local development to `127.0.0.1` and requiring explicit opt-in for LAN exposure.

## UI Recommendations

### Reduce broad dynamic `innerHTML`

`innerHTML` is used heavily for convenience. Many instances are escaped, but the pattern makes future mistakes likely.

Recommendation: reserve `innerHTML` for fixed static templates. Use DOM creation and `textContent` for repeated components: People, clips, conversation headers, search hits, and agent items.

### Add runtime UI smoke tests

`web/check_ui.sh` catches syntax, missing IDs, and simple scope issues. It cannot catch API shape drift or interaction bugs.

Recommendation: add a minimal Playwright test that stubs `/api/*` responses and exercises Device, Recordings, People, Notes, transcript detail, conversation reader, naming sheet, and edit sheet. Include strings with `<`, `>`, and `&`.

### Surface firmware capability differences

The web interface assumes one shared protocol. Zephyr and Arduino currently differ in some info fields and default behavior.

Recommendation: add a `firmware_caps` or `info_version` concept to the info characteristic. Until then, the UI should treat absent diagnostics as unavailable rather than zero.

## Data Model Recommendations

### Keep files authoritative, but formalize derived indexes

The current design, with WAV/transcript/voiceprint files as source of truth and SQLite as rebuildable index, is good.

Recommendation: document which files under `data/` are authoritative and which are cache/index:

- authoritative: WAV, transcripts, speaker samples/meta, vocabulary, agent JSONL, device-time sidecars
- derived: `index.db`, `semantic.db`, envelopes

### Add simulated stream tests

The highest-risk behavior is not UI rendering; it is sequence semantics:

- packet loss
- intentional VAD gaps
- replayed flash frames
- timestamp-based recovered clips
- ordering across variable clip durations

Recommendation: add pure Python tests for `Device.consume()`, `index_db.conversations()`, and shared frame parsing. No BLE hardware required.

## Suggested Web Work Order

1. Fix `index_db.conversations()` to use device start/end times consistently.
2. Fix People-tab escaping.
3. Add atomic write helpers and use them for transcripts/speaker metadata.
4. Return semantic indexing failures instead of dropping them silently.
5. Add request models for mutating API endpoints.
6. Add Playwright smoke tests with stubbed API responses.
