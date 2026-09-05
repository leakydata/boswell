# Brief: overhaul the Boswell web interface

You are redesigning the front end of a working product. The code is here and
runs; the backend is not the problem. What follows is the context you cannot
get by reading the code, and the constraints that are not negotiable.

Use your own judgement on everything else. Where this document and your taste
disagree about visual design, trust your taste. Where they disagree about how
the hardware behaves, trust this document.

---

## 1. What this thing is

Boswell is an always-on wearable audio recorder its owner built himself. A
microphone on a lanyard records continuously, streams to this computer over
Bluetooth, and the host transcribes it, works out who was speaking, and files
it into a searchable archive of conversations.

It is a personal alternative to Plaud and Omi, and the reason it exists is
ownership: the audio, the voiceprints, the transcripts and the model all stay
on the owner's machine. Nothing is uploaded anywhere.

**One user. His own device, his own recordings, his own house.** Not a team
tool, not multi-tenant, no onboarding funnel, no marketing surface. Assume
competence and assume he already knows what the device is — he built it.

The device can also record standalone to a 16 GB card for about three weeks
without any computer, and those recordings come back either by plugging it in
or over Bluetooth. That matters to the interface because it means recordings
arrive in two ways, at unpredictable times, sometimes hours after they
happened.

---

## 2. What the interface is actually for

Six tabs, and they serve genuinely different jobs. This is worth
understanding before rearranging anything.

| tab | id | what it is for |
|---|---|---|
| **Device** | `viewDev` | Is it working right now? Connect, arm, gain, storage, firmware. The live operational panel. |
| **Recordings** | `viewRec` | Individual clips: play, read the transcript, correct it, delete. |
| **People** | `viewPpl` | Voice identity. Name a voice, merge two that are the same person, correct a mis-attribution. |
| **Notes** | `viewNotes` | What an LLM agent extracted from conversations — facts, tasks, events — for review and keeping or discarding. |
| **Heard** | `viewHeard` | Non-speech sound the device tagged: a fan, music, a door. Ambient context. |
| **Clear out** | `viewTidy` | Bulk deletion of junk audio. Destructive, deliberately separate. |

The **Device** tab is the one that gets looked at daily and it is the one
under most pressure — it has grown a row per feature for months and is now a
long undifferentiated list of controls of wildly different importance and
frequency. "Am I recording?" and "what is the double-tap firmness threshold"
have the same visual weight. That is the single biggest thing to fix.

The other five are all list-and-detail views over a growing archive
(currently ~2,400 clips). They will only get longer.

---

## 3. Hard constraints — do not break these

**One file, no build step.** The entire UI is `web/static/index.html`, about
4,900 lines including inline `<style>` and `<script>`. There is no bundler,
no npm, no framework, and adding one is out of scope. It is served by FastAPI
from `web/server.py`. Vanilla JS and hand-written CSS. Keep it that way —
this runs on a machine its owner maintains alone, and a toolchain is a thing
that breaks at 2 a.m.

**`bash web/check_ui.sh` must pass**, and it is part of `./run_tests.sh`.
It enforces three rules, each of which exists because the mistake shipped and
broke the page silently:

1. the inline script must parse (`node --check`)
2. every `$("id")` / `getElementById("id")` must match an `id` in the markup —
   a reference to a missing element throws during setup and takes every
   feature after it down with it
3. every identifier used must be declared — one out-of-scope variable in the
   websocket paint path threw on every message and killed the live device
   panel with nothing on screen to say why

`$("x")` is the local helper for `getElementById`. `?.` marks a reference as
deliberately optional and the check honours it.

**`./run_tests.sh` must pass — 441 tests.** Several assert on interface
strings and element ids by name (`tests/test_device_files.py`,
`test_card_status.py`, `test_tap_controls.py`, `test_ota.py`). If you rename
or restructure, update those tests to match the new intent rather than
deleting them; several encode a real bug that must not come back, and the
comments say which.

**Auth.** `window.fetch` is wrapped to attach an `X-Boswell-Token` header on
same-origin requests. Do not bypass it. `/ws` needs a ticket from
`POST /api/ws-ticket`.

**Themes.** Support light and dark properly, including the "system" default
where no attribute is stamped on the root.

---

## 4. The live state model

The Device tab is driven by a websocket at `/ws`. On connect the server sends
`{type: "state", ...}` and then pushes the same shape on every change. Other
message types carry log lines and events.

State keys you will paint from — the full set is in `Device.state` and
`parse_info()` in `web/server.py`:

```
connected  armed  scanning  error  device_address
level  peak  frames  lost  rate  clip_seconds
gain  vad  led_level  led_mode  backlog_mode
tap_enabled  tap_thresh
card_free_mb  clock_set  device_epoch  card_pull_pct
has_ota  has_files  has_clock  caps  codec_name  boot_id
recovered_frames  recovered_seconds  silent_reconnects  info_errors
```

**Capability bits matter.** `has_ota`, `has_files`, `caps & 0x1000` and
friends say what *this* firmware build supports. Two different boards run
different builds — one has a card and a speaker, one does not. A control for
something the connected device cannot do must not be shown. This is already
done and must survive.

Commands go back over the same socket as `{cmd: "..."}`:
`connect disconnect arm gain vad tap_enabled tap_thresh clear_buffer
fast_charge mic_power_save led backlog_mode dfu save`

Everything else is REST — 79 endpoints. Read them from `web/server.py`; they
are all `@app.get`/`@app.post` with docstrings that explain intent.

---

## 5. The failure mode to design against

This interface's characteristic bug is **the control that appears to do
nothing**. Four separate instances of it turned up in one evening:

- Two importing-shaped buttons sat a few rows apart, and the one that was
  usually hidden was the one that got pressed. Report: *"I clicked Import and
  I'm not sure if anything happened."*
- A button labelled "Look" stayed labelled "Look" once the list was on
  screen, so pressing it again looked inert.
- A press that arrived while a poll was in flight was silently dropped by a
  busy flag.
- A radio timeout was rendered as "empty", which told the owner his
  recordings were gone when they were fine.

The through-line: **this device does slow, invisible things** — a Bluetooth
transfer takes a minute, transcription takes longer, a card listing takes a
round trip — and the interface has been bad at saying so. Design for
latency and for uncertainty as first-class states. "Working on it", "no
answer", "nothing there" and "not applicable to this device" are four
different things and have repeatedly been shown as one.

Related and unfixed: there is no consistent pattern for progress,
confirmation, or error. Some actions log to a text pane, some change a
label, some do nothing visible. Unify it.

---

## 6. What "production and mature" should mean here

The owner asked for this specifically. Interpret it as:

- **Hierarchy that matches frequency and stakes.** Recording state and
  battery are looked at constantly; tap firmness is set once a year. They
  should not look alike.
- **Density that respects an archive that only grows.** ~2,400 clips today.
  Lists need real information design — scanning, grouping by time, filtering
  — not an ever-longer column.
- **Destructive actions that look destructive** and are hard to hit by
  accident. There is a two-tap confirm pattern already (`bDiscard`, `dfubtn`)
  — keep the idea, make it consistent.
- **Honest state.** Never show a stale value as current. The device
  disconnects often and the difference between "0" and "unknown" matters.
- **Restraint.** This is an instrument, not a landing page. No hero, no
  marketing copy, no decoration that does not carry information.

Read `WEB_INTERFACE_REVIEW.md` in this repo — an earlier line-by-line review
with accessibility and correctness findings, many still open.

---

## 7. Things you cannot tell from the code

- The owner wears this device daily. He is usually looking at the interface
  to answer one of two questions: *is it recording right now?* and *what did
  it catch?* Everything else is occasional.
- The LED on the device is the only feedback while wearing it, and it is on
  his chest where he cannot see it. The interface is the real status display.
- Recordings arrive **out of order and late**. A conversation from this
  morning can land at 6 p.m. when the device is docked. Timelines must not
  assume arrival order is chronological — every clip carries its true capture
  time.
- Some clips have **no reliable timestamp at all** (recorded before the host
  ever told the device what time it was). Those carry `time_known: false` in
  `data/times/*.json`. Showing them as though their time were a fact is
  wrong; hiding them is worse.
- Speaker labels are **guesses with confidence scores**. A conversation
  carries `identified`; a speaker carries `decision` of `uncertain` and a
  margin. The interface should show attribution as provisional where it is,
  because acting on a wrong name is the failure that matters here.

---

## 8. How to work

- `./run_tests.sh` before and after. It is fast, ~3 seconds.
- The server runs as `systemctl --user restart boswell.service` on port 8000.
  There is live hardware attached; the device may be recording. Do not send
  device commands (`arm`, `dfu`, `clear_buffer`) as part of testing the
  interface.
- The page is no-cached in dev, so a reload picks up edits immediately.
- Commit in coherent pieces with messages that say *why*, matching the
  existing log's style — read `git log` for the register.
- Where you remove something, be sure it is unused: several controls look
  vestigial and are not.

Redesign freely. Restructure the markup, rewrite the CSS, reorganise the
tabs, change the interaction model. Keep the functionality, keep the checks
green, and keep it one file that a person can read.
