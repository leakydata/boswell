# Web Interface, Backend, and Host Review

Reviewed: 2026-08-19 at commit `9e61072`

## Scope and Method

This is a fresh line-by-line review of:

- `web/*.py`, `web/static/index.html`, and web check scripts.
- `host/*.py` and `host/*.sh`, because these programs share the BLE protocol,
  user data, relay transport, speaker database, and flashing workflow.
- `tests/test_sequence.py` and `run_tests.sh`.

Runtime recordings, databases, transcripts, model output, generated caches,
images, and vendored agent skills were not treated as authored application code.
Accessibility and interaction checks were cross-checked against the current
[Web Interface Guidelines](https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md).

## Executive Summary

Claude's changes improved timestamp-based conversation grouping, firmware
capability reporting, atomic JSON replacement, People-view DOM construction, and
no-hardware regression coverage. The current code still has serious security,
stored-XSS, recording-integrity, concurrency, and cross-firmware issues.

The browser/backend should not be exposed beyond loopback in its current state.
Address WB-01 through WB-10 before treating it as a dependable recorder or a
private LAN service.

## Critical Findings

### WB-01: Stored transcript content can execute as browser HTML

Evidence:

- `web/static/index.html:1255-1265` inserts FTS `h.snippet` directly with
  `innerHTML`.
- `web/index_db.py:196-214` returns SQLite snippets containing raw transcript
  text plus `<mark>` tags.
- `web/static/index.html:1947-1950` inserts speaker names/IDs into
  `chip.innerHTML`.
- `web/static/index.html:1961-1967` inserts per-line speaker labels into
  `innerHTML`.

Transcript text, manual speaker names, and model-produced labels are persisted
user data. A value such as an element with an event handler can execute when a
search result or transcript is rendered. The People tab was converted to safe
DOM construction, but these other sinks remain.

Recommendation: build these rows with DOM nodes and `textContent`. For search
highlighting, return structured text ranges or parse only the server-generated
`<mark>` boundaries while escaping every text fragment. Add a strict CSP as
defense in depth, but do not use CSP as the primary fix.

### WB-02: The service is open on every network interface by default

Evidence: `web/server.py:576-588`.

The default bind is `0.0.0.0`, while `BOSWELL_TOKEN` defaults to empty. The
warning is useful, but it does not prevent anyone on the reachable network from
reading recordings, editing/deleting transcripts, controlling capture, or
requesting buffer deletion.

Recommendation: default to `127.0.0.1`. Refuse startup on a non-loopback bind
unless a token is set, or require an explicit `BOSWELL_ALLOW_INSECURE_LAN=1`
override. Use TLS through a trusted reverse proxy when crossing a machine
boundary.

### WB-03: WebSockets do not validate `Origin`

Evidence: `web/server.py:1466-1524`.

Both `/ws` and `/ingest` authenticate only the optional query token. When no
token is configured, a malicious web page opened in the user's browser can
attempt a WebSocket connection to localhost or a LAN address and issue control
commands. Browser same-origin rules do not automatically protect WebSockets at
the server.

Recommendation: validate the `Origin` header against an explicit allowlist and
reject missing/unexpected browser origins. Give relay clients a separate
credential and protocol identity rather than sharing browser access policy.

### WB-04: Agent clear accepts a path-bearing `kind`

Evidence: `web/server.py:1335-1339` and
`web/agent_runner.py:301-309`.

`api_clear_items()` does not validate `kind`, then `clear_items()` joins it
into `data/agent/{kind}.jsonl` and removes the resulting path. Query strings can
contain slashes, so a crafted kind can traverse to another reachable JSONL file.
The single-item delete endpoint has the allowlist that this endpoint lacks.

Recommendation: accept only `tasks`, `events`, `notes`, and `facts` at every
agent boundary, including reads. Resolve the final path and assert its parent is
exactly the agent store before deleting.

### WB-05: Arduino state reporting makes the web service clear live audio every second

Evidence:

- `web/server.py:371-383` treats info byte 5 bit 2 as authoritative capture
  state and sets `_rearm_needed` when it is absent.
- `web/server.py:351-363` sends `CTRL_STREAM=1` on every later info poll.
- Arduino `firmware/ble_mic/ble_mic.ino:372-389` never sets bit 2.
- Arduino's stream command resets `ringTail=ringHead` at
  `ble_mic.ino:426-435`.

When the web service talks to Arduino, it can repeatedly discard all samples
currently waiting in the microphone ring. This is a cross-layer data-loss bug
introduced by trusting a field that only Zephyr currently publishes.

Recommendation: add a capability bit for capture-state validity and only re-arm
when that bit is present. Fix Arduino to publish its actual state. Add a protocol
contract test that feeds both 40-byte info layouts through the real parser.

### WB-06: Clip finalization can lose or overwrite recordings

Evidence: `web/server.py:139-176`, `298-309`.

- Live and recovered filenames have only one-second resolution, so two saves in
  the same second can overwrite one another.
- `take_clip()` empties `self._pcm` before `sf.write()`; a write failure loses
  the only in-memory copy.
- WAV files are written directly to their final names.
- Shutdown cancels tasks without flushing partial live/recovered audio
  (`web/server.py:560-572`).

Recommendation: generate collision-resistant UTC names, write to a same-directory
temporary file, flush/fsync, atomically replace or link with no-overwrite
semantics, and clear memory only after success. On graceful shutdown, stop
ingest, finalize both streams, index successful clips, and await task
cancellation.

### WB-07: Recovered timestamps are not reliable across reconnect, reboot, or wrap

Evidence: `web/server.py:139-175`, `211-247`, `265-296`, and
`326-369`.

`_recovered_start` is initialized and read but never assigned. A backlog can be
rotated before any live frame establishes the wall/device clock anchor. The
anchor is not reset at session start, so a new board boot can inherit an old
mapping. Device timestamps are 32-bit but min/max and subtraction are not
wrap-aware.

Recommendation: add a firmware boot/session ID, reset clock mapping on every
source session, use wrap-aware timestamp expansion, and persist the mapping used
for each sidecar. Do not finalize recovered audio until it has an authoritative
anchor, or mark its time explicitly unknown rather than stamping drain time.

### WB-08: Local BLE and relay ingest can corrupt one shared session

Evidence: `web/server.py:73-114`, `312-369`, `433-450`, and
`1466-1516`.

The local BLE loop starts automatically while any relay may also connect. Both
write into the same PCM arrays, sequence counters, timestamp anchors, state, and
source label. A second relay overwrites `device.relay`; when the first relay
disconnects, it clears the second relay's reference.

Recommendation: model an explicit ingest session with a unique ID and exactly
one owner. Reject or queue duplicate sources, bind callbacks and disconnect
cleanup to the session ID, and keep source-specific buffers/counters.

## High Findings

### WB-09: Token login exists but is never invoked

Evidence: `web/static/index.html:671-677`, `781-806`.

`ensureAuth()` is defined but startup calls `connect()` directly. With a token
configured and none stored, the WebSocket is rejected and reconnects forever;
the token gate is never shown.

Recommendation: await authentication before constructing the app session. Render
the gate with an actual label, focus the token input, announce errors, and stop
the reconnect loop after an authentication close code.

### WB-10: Query-string/localStorage token handling increases exposure

Evidence: `web/static/index.html:654-668`, `781-784`;
`web/server.py:621-624`, `1479`, `1521`; `host/relay.py:31-34`.

Tokens in WebSocket URLs can enter access logs and diagnostics. A long-lived
token in `localStorage` is readable by any successful XSS, including WB-01.
The relay also concatenates the token without URL encoding.

Recommendation: exchange the token over an authenticated HTTP request for a
Secure, HttpOnly, SameSite cookie or a short-lived single-use WebSocket ticket.
Use structured URL construction in the relay and redact credentials from logs.

### WB-11: Manual save bypasses the normal clip workflow

Evidence: `web/server.py:1559-1562` versus `178-210`.

The WebSocket `save` command writes a clip but does not index it, emit a clip
event, enqueue transcription, or report a write error like automatic rotation.

Recommendation: factor one finalization function that performs write, sidecar,
index, event, and optional transcription atomically enough to report partial
failure.

### WB-12: Agent failures permanently discard scheduled work

Evidence: `web/agent_runner.py:118-136`.

The scheduled batch is removed from `_pending` before `_run()`. An Ollama
timeout or tool exception only logs; the batch is never requeued.

Recommendation: retain an in-flight batch and requeue it with bounded exponential
backoff. Persist pending work if it must survive process restart, and expose a
dead-letter/error state after repeated failures.

### WB-13: Manual agent review can return an unrelated prior result

Evidence: `web/agent_runner.py:103-111`, `192-203`.

`review_now()` returns global `last_result`. If the selected text is shorter
than `MIN_CHARS`, `_run()` returns without changing that field, so the API can
return the result from a previous conversation.

Recommendation: have `_run()` return a result object for that invocation,
including an explicit `skipped_reason`, and let scheduled callers separately
update `last_result`.

### WB-14: Agent JSONL append and rewrite operations can lose each other

Evidence: `host/tools_impl.py:14-30` and
`web/agent_runner.py:260-309`.

Agent tools append while edit/delete/clear rewrites or removes the whole file.
There is no shared lock, so an append that races a rewrite can disappear.

Recommendation: put agent records in SQLite with transactions, or centralize all
access behind one lock and one storage module. Validate and fsync the parent
directory after replacement.

### WB-15: Speaker data updates are non-atomic and non-transactional

Evidence: `web/pipeline.py:55-73`, `101-119`, `185-214`.

NPZ files are written directly, and samples, metadata, and centroids are three
separate updates with no lock. A crash or concurrent API request can leave the
files inconsistent. Sample IDs based on millisecond time can also collide under
concurrency.

Recommendation: serialize speaker mutations, use UUID sample IDs, write each NPZ
through a same-directory temporary file, and commit a generation manifest last.
SQLite/BLOB storage would provide a cleaner transaction boundary.

### WB-16: The legacy host speaker tool can overwrite the web database schema

Evidence: `host/speaker_db.py:26-76` versus `web/pipeline.py:34-214`.

The CLI writes the old centroid/meta representation directly to the same
`data/speakers.npz` and `data/speakers.json` used by the newer sample-based web
store. Running it after web enrollment can discard sample metadata and make the
two files disagree.

Recommendation: remove the duplicate persistence implementation. Import one
shared speaker-store module from both CLI and web, with migration tests.

### WB-17: Semantic replacement leaves stale search rows

Evidence: `web/semantic.py:85-132` and transcript mutation endpoints at
`web/server.py:1184-1256`.

`replace=True` upserts current segment indices but does not delete rows for
segments that were removed or shortened. Transcript edit/revert updates the FTS
index inconsistently and does not update semantic vectors. On an UPSERT,
`cur.lastrowid` is not a reliable way to identify the conflict row.

Recommendation: replace a clip in one transaction: delete all of its semantic
rows, insert the new set, and rebuild vector rows using IDs selected explicitly.
Invoke this path after edit, revert, split, and retranscription.

### WB-18: Raw filenames and request bodies are validated inconsistently

Evidence: `web/server.py:797-855`, `878-894`, `1114-1178`,
`1184-1235`, and `1394-1444`; `web/pipeline.py:336-337`.

Some endpoints reject `/`, others accept body-provided names directly, and
`transcript_path()` does not enforce a basename or extension. Speaker IDs can
also enter split output filenames. This creates path traversal opportunities in
body-based APIs, malformed output paths, and avoidable 500 responses.

Recommendation: use one resolver that requires `Path(name).name == name`, a
`.wav` extension, and a resolved parent equal to `DATA`. Sanitize generated
components and use Pydantic models with length/type/enum limits for every
mutating request.

### WB-19: Deleting clips leaves authoritative timestamp sidecars behind

Evidence: `web/server.py:1058-1111`.

Single and bulk deletion remove WAV, transcript, and envelope files but not
`data/times/{name}.json`. Orphan sidecars can later be mistaken for valid timing
metadata if a filename is reused.

Recommendation: define and use one derived-artifact list for all delete paths,
including time sidecar and semantic rows.

### WB-20: Transcription queue accepts duplicate work

Evidence: `web/pipeline.py:340-355` and submit callers.

Automatic rotation, individual requests, conversation requests, and bulk actions
can enqueue the same clip repeatedly. Expensive ASR/diarization then runs more
than once, and later runs can overwrite edited output depending on timing.

Recommendation: track queued and in-progress clip names under a lock, return
`already_queued`, and recheck edit protection immediately before processing.

### WB-21: Vocabulary changes reload a model that does not consume vocabulary

Evidence: `web/pipeline.py:357-382`.

The vocabulary signature forces ASR reload, but the load/transcribe calls pass no
hotwords or prompt; vocabulary is applied in post-processing. Reloading while the
old GPU model is still referenced can temporarily double memory use and OOM.

Recommendation: do not reload ASR for post-processing vocabulary changes. If a
future decoder configuration uses vocabulary, release old model references and
GPU cache before loading replacements.

### WB-22: FTS query construction can throw on valid user input

Evidence: `web/index_db.py:196-208`.

Each whitespace token is wrapped in quotes without escaping embedded quotes or
other FTS syntax. A query containing a quote can raise `sqlite3.OperationalError`
and return a server error.

Recommendation: escape FTS phrase quotes, use a small tested query builder, and
convert syntax failures into a 400 response. Add tests for quotes, punctuation,
Unicode, empty input, and very long input.

### WB-23: Info parsing uses capabilities from the previous read

Evidence: `web/server.py:371-426`.

Steps/motion are parsed before bytes 18-21 update the firmware identity and
capabilities. On the first read, defaults suppress steps; across firmware changes
or reconnects, stale capabilities can interpret Arduino tap bytes as Zephyr
motion. Motion flags are parsed even when step capability is absent.

Recommendation: parse version/capabilities first into local variables, then
decode only fields enabled by those local capabilities. Reset all optional state
at session start.

### WB-24: BLE read errors terminate the entire session

Evidence: `web/server.py:351-364`.

A single transient info-characteristic read exception escapes the session loop,
disconnecting and restarting discovery even if notifications were healthy.

Recommendation: catch and count per-read failures, retain the audio session, and
reconnect only after a threshold or confirmed link loss.

### WB-25: Sample-rate changes can mix incompatible PCM in one clip

Evidence: shared `self._pcm` and mutable `state["rate"]` in
`web/server.py:211-305`.

If the device rate changes while a partial clip exists, old and new samples are
concatenated and written under the latest rate.

Recommendation: rotate before applying a rate change, or attach rate/session
metadata to every buffered frame and reject mixed-rate finalization.

### WB-26: `int16` absolute peak can overflow

Evidence: `web/server.py:258` and `host/ble_capture.py:211`.

`abs(-32768)` in signed 16-bit arithmetic remains negative, so a full-scale
negative sample can produce an incorrect peak.

Recommendation: cast PCM to `int32` or float before `abs().max()`. Add a test
containing exactly `-32768`.

## Host and Script Findings

### WB-27: Relay queue overflow is caught in the wrong thread

Evidence: `host/relay.py:42-47`.

`call_soon_threadsafe()` schedules `put_nowait`; any `QueueFull` is raised later
on the event loop, outside the surrounding `try`. The callback can emit noisy
unhandled exceptions while drops are not counted.

Recommendation: schedule a wrapper that catches `QueueFull` on the event loop
and increments a relay-drop counter. Add reconnect/backoff and publish actual
armed state in relay status (`host/relay.py:71-85` currently omits it).

### WB-28: Standalone BLE capture mixes live and replay audio

Evidence: `host/ble_capture.py:95-135`, `201-229`.

Gap statistics separate replay and live frames, but both are appended to one WAV
in arrival order. Catch-up mode can interleave different moments. Output rate is
selected from the CLI flag rather than the device's post-control info, and replay
frames inflate the real-time accounting denominator.

Recommendation: write live and recovered streams separately, use the actual frame
rate/capability, and compute timing statistics per stream.

### WB-29: Host protocol parsers need hard bounds

Evidence: `host/ble_capture.py:44-71`,
`host/imu_capture.py:62-83`, and other capture utilities.

ADPCM index/sample count and IMU count/stride are not fully validated before
indexing or `struct.unpack`. Corrupt or incompatible frames can raise in a BLE
callback and stop useful processing.

Recommendation: centralize validated audio/info/IMU parsers with maximum lengths,
known flags, and structured errors. Make tests call those real parsers instead
of local replicas.

### WB-30: Serial resynchronization can wait forever

Evidence: `host/capture_serial.py:29-30`.

The initial loop that fills a two-byte sync window has no empty-read deadline, so
a silent port can block indefinitely before the later timeout logic runs.

Recommendation: apply one monotonic deadline to every read phase and report
timeout distinctly from bad framing.

### WB-31: OTA package assumptions are not enforced

Evidence: `host/ota_update.py`.

The package loader can fall back to a SoftDevice section while the transfer path
still uses application-update semantics. The init packet is also written as one
GATT value without proving it fits the negotiated legacy-DFU limit.

Recommendation: reject unsupported image types, validate manifest/hash/size, and
chunk init/data according to the exact bootloader protocol and negotiated MTU.
Test against a recorded DFU exchange before using unattended updates.

### WB-32: Flash scripts can report success after a failed copy

Evidence: `host/flash.sh:5-32` and `host/flash_any.sh:9-31`.

Both use `set -u` without `-e` or explicit copy-result handling. A failed
`sudo cp ... && sync` can be followed by sleep, unmount, a success message, and
exit zero. The fixed mountpoint also lacks a cleanup trap.

Recommendation: use `set -euo pipefail`, a temporary mount directory, and an
`EXIT` trap. Verify the destination file or booted firmware identity before
reporting success.

### WB-33: The test wrapper masks pytest failure

Evidence: `run_tests.sh:20`.

`uv run ... | tail -3 || fail=1` returns `tail`'s status because pipefail is
off. During this review, `uv` failed to create its default cache file and the
script still exited zero.

Recommendation: enable `set -o pipefail`, or capture output and check pytest's
actual status before printing the tail. Set a writable `UV_CACHE_DIR` in
restricted/CI environments.

### WB-34: Several CLI data paths depend on the current directory

Evidence: `host/tools_impl.py:11`, `host/agent.py:29-31`, and
`host/speaker_db.py:26-27`.

Running tools outside the repository root can read or create a different
`data/` tree.

Recommendation: derive paths from `__file__` or accept one explicit data-root
option shared by every CLI and web process.

## Accessibility and Interaction Findings

### WB-35: Closed sheets remain in the accessibility and tab order

The edit/naming sheets are moved offscreen with transforms rather than being
`hidden` or `inert`. They lack dialog semantics, focus trapping, Escape close,
and reliable focus restoration.

Recommendation: use native `<dialog>` or implement `role="dialog"`,
`aria-modal`, `hidden/inert`, initial focus, focus trap, Escape handling, and
return focus to the invoking control.

### WB-36: Tabs implement clicks but not the tab keyboard pattern

Evidence: tab handling around `web/static/index.html:808-824`.

Tabs do not expose `aria-controls`/tabpanel relationships or arrow-key and
roving-tabindex behavior. Active tab, search, filters, selected conversation, and
detail state are not reflected in the URL, so refresh/back navigation loses user
context.

Recommendation: implement the ARIA tabs pattern and encode meaningful navigation
state in URL parameters/history.

### WB-37: Several controls are not keyboard-equivalent or programmatically labeled

Examples include the custom agent switch, the clickable conversation text at
`index.html:1643-1649`, vocabulary/editor textareas, naming input, and token
input. Placeholder text is not a label. Generic `role="switch"` spans require
explicit names and keyboard behavior.

Recommendation: prefer native checkbox/button controls, add visible or
screen-reader labels, and ensure Enter/Space performs every click action.

### WB-38: Page landmarks, announcements, and focus visibility are incomplete

There is no skip link or `<main>` landmark. Async error/status regions are not
consistently `aria-live`. Sticky navigation can obscure focused content, and
some input styles use `:focus` rather than `:focus-visible`.

Recommendation: add landmarks and skip navigation, live regions for job/error
updates, `scroll-padding-top`, and consistent `:focus-visible` rings. Warn
before closing transcript/vocabulary editors with unsaved changes.

### WB-39: Large clip lists render all rows at once

`index_db.list_clips()` can return 1,000 records and the browser constructs every
button. This can become expensive on mobile.

Recommendation: paginate or virtualize. A low-cost intermediate improvement is
`content-visibility: auto` with stable intrinsic row sizing.

### WB-40: External Google Fonts violate the local-only privacy claim

Evidence: `web/static/index.html:8-10`.

Loading Google Fonts sends browser/network metadata to a third party even though
the project describes itself as local-only.

Recommendation: self-host the font files or use a system-font stack. Add a CSP
that makes unexpected external connections visible during development.

## Validation Results

- `bash web/check_ui.sh`: passed; JavaScript syntax, 126 referenced elements,
  and current scope checks passed.
- Python syntax check for every `web/*.py` and `host/*.py`: passed.
- Direct `uv run python -m pytest tests/ -q` with a writable cache: **12 passed**.
- `run_tests.sh`: exited zero even when its pytest command failed, confirming
  WB-33.
- No board-dependent BLE, relay, DFU, microphone, transcription-model, or
  browser-runtime test was claimed.

## Positive Changes to Preserve

- Device-time sidecars and consistent conversation grouping fix a real ordering
  failure.
- Capability bits are a good basis for firmware-specific info parsing.
- Atomic JSON replacement is now used in several important paths.
- People-view rendering uses DOM/text nodes instead of interpolated HTML.
- Files remain authoritative while SQLite indexes remain rebuildable.
- The tests focus on silent ordering and durability regressions.

## Recommended Work Order

1. Remove stored-XSS sinks and lock network exposure to loopback (WB-01 to WB-03).
2. Fix path validation and cross-firmware capture-state handling (WB-04, WB-05).
3. Make clip writes/finalization and recovered timing reliable (WB-06, WB-07).
4. Enforce one ingest session and repair auth bootstrap/token transport (WB-08 to WB-10).
5. Transactionalize agent, speaker, and semantic storage (WB-11 to WB-20).
6. Centralize protocol parsing and fix host/flash/test tooling.
7. Add browser-level accessibility and workflow tests.

## Files Reviewed

- All authored files under `web/`
- All authored Python and shell files under `host/`
- `tests/test_sequence.py`
- `run_tests.sh`
- Firmware protocol publishers referenced by the host
