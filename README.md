# Boswell

**An open, fully-local wearable that listens, transcribes, knows who is speaking, and acts on what it hears.**

No cloud. No account. No audio ever leaves your machine.

---

## Why "Boswell"

James Boswell spent two decades following Samuel Johnson around London writing
down his conversations verbatim, and published them as *The Life of Samuel
Johnson* — still one of the most detailed records of how any person actually
talked. The word entered English as a common noun: **a Boswell is a devoted
recorder of another's conversation.**

That is precisely what this is. A small device that quietly keeps the record,
so you don't have to.

---

## The interface

![Device tab](docs/ui-device.jpg)

*Connection and capture state, input level, flash backlog, and live controls for
gain, voice gating, charge current and the status light.*

![A transcript](docs/ui-transcript.jpg)

*Each voice gets its own colour in the waveform, so you can see who spoke when
without cutting the conversation apart. Everything that is not speech stays grey.
Playback lives in the waveform: tap anywhere to seek.*

![Recordings](docs/ui-recordings.jpg)  ![People](docs/ui-people.jpg)

*Recordings can be filtered to those with voice or without. Naming a speaker
enrols their voiceprint, and the People tab shows what each one was built from.*

> Screenshots use synthetic speech, not real recordings.

## Status

Working prototype. Every stage below has been run end-to-end on real hardware
with real conversations.

| Stage | Status |
|---|---|
| PDM mic → USB → WAV → transcript | ✅ |
| PDM mic → **BLE** → WAV → transcript | ✅ |
| Mic gain calibration | ✅ |
| On-device VAD (hangover + pre-roll) | ✅ |
| Speaker diarization | ✅ |
| **Named speakers** via voiceprint enrollment | ✅ |
| Local LLM agent with tool calling | ✅ |
| QSPI store-and-forward buffering | ✅ |
| IMU double-tap toggle + RGB status LED | ✅ |
| IMU: pedometer, activity detection | ⬜ planned |
| Responsive web UI | ✅ |
| Phone app | ⬜ planned |

---

## Hardware

| Part | Notes |
|---|---|
| **Seeed XIAO nRF52840 Sense** | nRF52840, 1 MB flash / 256 kB RAM, BLE 5.4 |
| PDM microphone | MSM261D3526H1CPM, on-board |
| 6-axis IMU | LSM6DS3TR-C, on-board |
| 2 MB QSPI flash | P25Q16H, on-board |
| Charger | BQ25101 — **50 mA fast charge**, see notes |
| Host | Any Linux box with a CUDA GPU |
| BLE adapter | Bluetooth 4.0 is sufficient (see measurements) |

Firmware uses **14% of flash and 10% of RAM**, so there is ample room for the
planned features.

---

## Hardware reference

Official documentation: [Seeed wiki](https://wiki.seeedstudio.com/XIAO_BLE/) ·
[datasheet (PDF)](https://files.seeedstudio.com/Bazaar/product_pdf/102010469.pdf)

Pin assignments this firmware depends on, from the board's Arduino variant:

| Function | Arduino pin | nRF52840 | Notes |
|---|---|---|---|
| PDM power | D19 | P1.10 | |
| PDM clock | D20 | P1.00 | |
| PDM data | D21 | P0.16 | |
| IMU power | D15 | **P1.08** | **must be H0H1 high drive** |
| IMU I²C SCL | D16 | P0.27 | dedicated bus, not the D4/D5 header |
| IMU I²C SDA | D17 | P0.07 | |
| IMU INT1 | D18 | P0.11 | double-tap interrupt |
| LED red / green / blue | D11 / D13 / D12 | P0.26 / P0.30 / P0.06 | active low |
| Battery sense enable | D14 | P0.14 | drive **low** to read |
| Battery voltage | — | P0.31 | 1M/510k divider |
| Charge current select | D22 | P0.13 | low = 100 mA, float = 50 mA |
| Charge status | D23 | P0.17 | active low, **not named in the variant** |
| QSPI flash | D24–D27 | P0.21/25/20/24 | P25Q16H, 2 MB |

### Things the documentation does not tell you

**The IMU will not power up on a normal output pin.** Its supply is P1.08 and
the pin must be configured `H0H1` high drive. Standard drive cannot source
enough current, so the sensor never starts and every I²C probe reads `0xFF`,
which looks exactly like a wiring fault.

**The IMU is on its own I²C bus** (P0.27/P0.07), not the D4/D5 header pins. The
board's own library papers over this with `#define Wire Wire1`.

**The nRF52 core's I²C blocks forever.** Its TWIM driver spins on hardware
events with no timeout, so one unresponsive device wedges the main loop while
Bluetooth keeps advertising — the board looks alive and has silently stopped
recording. This firmware bit-bangs I²C instead, where every loop is bounded.

**Rate-limit sensor polling by wall clock, not loop iterations.** A poll written
as "every fourth pass through `loop()`" becomes hundreds of transactions per
second once the loop is spinning on audio. That was enough here to drown the
IMU's own tap interrupt and, with one more poller added, to crash the firmware
at boot.

**`PDM.begin()` resets microphone gain.** It calls `setGain(20)` internally, so
gain set beforehand is silently discarded. Always set it afterwards.

**The QSPI part is not in the flash-device table.** Every Seeed nRF52 variant
names `P25Q16H` but `Adafruit_SPIFlash` has no entry for it, so it must be
declared by hand (see `firmware/ble_mic/qspi_store.h`).

**Charging defaults to 50 mA**, which is about ten hours for a 500 mAh cell.
The BQ25101's HICHG pin selects 100 mA.

## Architecture

```
XIAO nRF52840 Sense
  PDM mic 16 kHz
    → 2:1 decimate (optional)      8 kHz
    → energy VAD + 300 ms hangover + 60 ms pre-roll
    → IMA ADPCM 4:1
    → BLE GATT notify              ~32 kbps
                │
                ▼
Host (Linux + CUDA)
    → ADPCM decode → WAV
    → WhisperX large-v3            transcript + word timestamps
    → pyannote diarization         SPEAKER_00 / SPEAKER_01
    → voiceprint match             → real names
    → chunk on pauses & turns
    → local LLM (tool calling)     → notes / tasks / events / facts
```

### Frame format

Each BLE frame carries **its own ADPCM predictor state**:

```
[seq:u16][flags:u8][stepIndex:u8][predictor:i16][nsamples:u16][nibbles...]
```

Standard ADPCM shares encoder state across the whole stream, so a single lost
packet desynchronises the decoder permanently. Over a radio link that is fatal.
Self-contained blocks cost 8 bytes per frame and make packet loss cost exactly
one frame.

---

## Measured performance

Real numbers from real runs, not estimates.

| Metric | Value |
|---|---|
| Over-the-air bitrate | **35.2 kbps** (8 kHz ADPCM) |
| Frame loss, Bluetooth **4.0** dongle | **0.00%** over 1501 frames |
| VAD airtime reduction | **52.9%** → 15.9 kbps average |
| Transcription speed | ~20× realtime, Whisper large-v3 on an RTX 4090 |
| Same-speaker embedding similarity | 0.65 – 0.87 |
| Flash backlog drain rate | **~4.3× realtime** |
| Offline buffer capacity | ~7.7 min continuous, more with VAD |
| Different-speaker similarity | 0.38 – 0.48 |

**8 kHz ADPCM at 32 kbps is enough for accurate transcription.** This was the
open design question, and it means Opus is an optimisation for battery life
rather than a requirement — a large amount of firmware complexity avoided.

A Bluetooth 5.x adapter is *not* required. It buys headroom for 16 kHz (64 kbps)
and eventual Opus, but a cheap BT 4.0 dongle dropped nothing.

---

## Store-and-forward

Capture is *armed*, not *connected*. When the host goes away the device keeps
recording into the onboard 2 MB flash, and drains the backlog on reconnect
before resuming live audio, so the conversation arrives in order rather than
with a hole in it.

Measured over a 20-second outage: 90,990 bytes buffered (20.2 s of audio),
drained in 4.7 s. The recovered audio is statistically indistinguishable from
live audio.

Records are `[0xB5][len][payload]` in a circular byte stream. The magic byte
matters: when the writer laps the reader the oldest sector is dropped, which
can leave the read pointer mid-record, and scanning for the magic recovers the
stream instead of emitting garbage.

When a host reconnects to a backlog you choose what happens, from the Device
tab:

- **Drain first** (default) — finish the buffered audio before any live audio,
  so the conversation reaches you in order. Live capture waits, which on a long
  outage means a real delay before you hear the present.
- **Play catch-up live** — live audio starts immediately and one buffered frame
  is sent alongside each live frame, so the backlog empties at roughly realtime
  without ever starving the live stream. Recovered frames are flagged and saved
  as their own `recovered_*.wav`, since they belong to an earlier moment and
  splicing them into live audio would produce a recording of something that
  never happened.
- **Discard buffer** — throw the backlog away and go live at once.

Measured in catch-up mode: live audio advanced 5.0 s → 9.5 s of wall clock while
recovered audio advanced 1.0 s → 5.2 s and the backlog fell 76.1 s → 72.1 s.

Buffering to flash means erasing a 4 kB sector roughly every 0.9 seconds of
audio, and a NOR erase blocks the CPU for tens of milliseconds — up to 300 ms
on this part. The microphone keeps producing samples throughout, so the ring
buffer has to absorb a whole erase or it overruns, and dropped samples are
audible as a click. At 256 ms of headroom it could not: buffered recordings
carried 681 discontinuities spaced a mean of 0.93 s apart, against a predicted
erase period of 0.91 s. The ring is now 16384 samples, just over a second, and
the same test measures zero.

The status LED reports where audio is going: blue advertising, **magenta
buffering to flash**, cyan draining the backlog, green streaming live, red
connected but disarmed.

## Named speakers

Diarization only produces per-file labels — `SPEAKER_00` in one recording is
not the same person as `SPEAKER_00` in another. Boswell maps those to real
people by cosine-matching 256-dim voiceprints against an enrollment database.

**There is no training step.** Enrolling appends to a running centroid in
embedding space. It works from the first sample and sharpens with each label.

```bash
uv run host/speaker_db.py enroll alice data/spk_meeting.npz SPEAKER_00
uv run host/speaker_db.py identify data/spk_other.npz
```

This also gives you background rejection for free: a voice from a television
or radio matches nobody enrolled, scores below threshold, and is reported as
`UNKNOWN`.

---

## Quick start

```bash
# 1. environment (uv)
uv venv --python 3.11 .venv
uv pip install numpy scipy pyserial bleak soundfile requests intelhex
uv pip install fastapi "uvicorn[standard]"
uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
uv pip install whisperx

# 2. firmware
arduino-cli core install Seeeduino:nrf52
arduino-cli compile --fqbn Seeeduino:nrf52:xiaonRF52840Sense \
    --output-dir build_ble firmware/ble_mic
adafruit-nrfutil dfu serial -pkg build_ble/ble_mic.ino.zip \
    -p /dev/ttyACM0 -b 115200 --singlebank

# 3. capture over BLE
uv run host/ble_capture.py --scan
uv run host/ble_capture.py --seconds 30 --out data/voice.wav

# 4. transcribe + diarize
export HF_TOKEN=hf_...
uv run host/transcribe.py data/voice.wav --diarize \
    --save-embeddings data/spk_voice.npz

# 5. agent
ollama serve &
uv run host/agent.py data/voice.wav
```

### Web UI

```bash
uv run web/server.py     # then open http://localhost:8000
```

A local service that owns the Bluetooth link and serves the interface. It
connects to the board on startup, so a restart resumes capture without
intervention.

**Device** — connection and capture state, input level, and the device's flash
backlog with drain progress. Microphone gain and VAD are adjustable live over
GATT, no reflash.

**Recordings** — every clip, newest first, with a preview of what was said.
Clips are written every 30 seconds and transcribed automatically; nothing needs
a tap per clip.

Filter by content (all, with voice, silent) and by date (any time, today, last
seven days, older). **Select** turns on multi-select for deleting in bulk, with
*Select all shown* respecting whichever filters are active — so "delete every
silent clip older than a week" is three taps. Deleting recordings never touches
enrolled voices; those live in their own files.

Opening a clip gives you:

- a **waveform coloured by speaker** — each voice a different colour, everything
  that is not speech grey. Colour comes from the transcript, never from
  loudness, so a slammed door stays grey.
- **playback in the waveform itself** — play/pause, elapsed and total time, and
  tapping the waveform seeks there.
- the **transcript**, tinted to match, with clickable timestamps.
- **speaker chips** showing who spoke, for how long, and the match confidence.
  Tapping one names that voice.
- **split by voice**, which writes one single-voice clip per speaker. The
  original is untouched; the splits exist because a voiceprint taken from one
  person alone is much stronger than one taken from overlapping speech.
- **delete**, which also removes the transcript and cached waveform.

All device state and every action cross a single WebSocket as JSON.

### Remote access

The web UI is unauthenticated by default, which is fine on a trusted LAN.
Before exposing it anywhere else, set a token — every endpoint can arm the
microphone or hand over recordings:

```bash
export BOSWELL_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
uv run web/server.py
```

The page then asks for the token once and remembers it. HTTP and WebSocket
are both gated; only the page shell and its assets stay public so the prompt
can render.

A token is not a substitute for a private network. Prefer **Tailscale or
WireGuard**, where the device is reachable as if it were at home and nothing
is published; a tunnel like ngrok puts a microphone control API on the public
internet, and tunnel URLs get scanned within hours of being created.

### Tuning

```bash
uv run host/tune_gain.py --seconds 4          # sweep mic gain, find levels
uv run host/analyze_vad.py data/sample.wav    # derive a VAD threshold
```

---

## Gotchas worth knowing

Things that cost real debugging time, documented so they don't cost you any.

**`PDM.begin()` silently resets mic gain.** The Arduino PDM library calls
`setGain(DEFAULT_PDM_GAIN)` *inside* `begin()`. The stock `PDMSerialPlotter`
example — which everyone copies — sets gain before `begin()`, so it is
overwritten and has no effect. **Always call `setGain()` after `begin()`.**
This cost us 10 dB of signal and badly degraded speaker embeddings while
leaving transcripts looking deceptively fine.

**Poor SNR wrecks speaker ID long before it wrecks transcription.** A recording
at −10 dB transcribed perfectly but failed to match its own speaker (0.49 vs
0.87 for a correctly-levelled recording). If speaker identity matters, get the
levels right.

**pyannote.audio 4.x ignores which pipeline you request.** Even when you ask
for `speaker-diarization-3.1`, it downloads assets from
`pyannote/speaker-diarization-community-1`, which is gated *separately*. You
must accept that repo too.

**Check HuggingFace access with file downloads, not the model API.**
`GET /api/models/<repo>` returns 200 for gated repos you can merely see. Use
`GET /<repo>/resolve/main/config.yaml` instead.

**whisperx `DiarizationPipeline` takes `token=`**, not `use_auth_token=`, and
its `__call__` accepts `return_embeddings=True` — so no separate embedding
model is needed for enrollment.

**Speech/silence separation in a normal room is only ~9 dB.** A bare energy
threshold chops word onsets, because quiet consonants (`f`, `s`, `th`) fall
below it. Hangover plus pre-roll is what makes energy VAD usable. Mounting the
mic at the collar rather than on a desk is worth 10–15 dB and helps far more
than any code change.

**Rate-limit sensor polling by wall clock, never by loop iterations.** A poll
written as "every 4th pass through `loop()`" became several hundred bit-banged
I2C transactions per second once the loop was spinning freely on audio. That
was enough to drown the LSM6DS3's own tap interrupt — taps were never seen —
and adding one more poller on top pushed the firmware into crashing at boot,
where the bootloader caught it and the board went completely dark. Polling on a
50 ms timer fixed both symptoms at once. The latched interrupt (`LIR`) means a
slow poll cannot miss an event.

**The IMU supply pin needs high drive.** It is P1.08 and must be configured
`H0H1`; a plain `pinMode(OUTPUT)` cannot source enough current, so the sensor
never starts and every I2C probe reads `0xFF`. Nothing in the board variant
header hints at this.

**Tap detection needs the board free to move, not held still.** A device lying
flat on a desk barely registers taps on its face -- the desk absorbs the
impulse and the accelerometer sees very little. The same taps delivered to the
side, or with the board held loosely, trigger reliably. Worn in a case this is
a non-issue, but it makes a desk the worst possible place to test, and it is
easy to misread as a broken configuration.

Default tap threshold is 3 (~187 mg). It can be changed live over the control
characteristic (`0x07 <n>`) without reflashing, so it is worth dialling in on
the finished enclosure rather than on bare hardware. Going much lower risks a
bump toggling capture off while worn, which fails silently.

**The 50 mA charge limit is the BQ25101, not the battery.** If your enclosure
has room, charge through an external charger rather than the on-board one.

---

## Privacy and consent

Boswell records conversations continuously. That is the point, and it carries
real responsibility.

- **All processing is local.** Audio, transcripts, and voiceprints never leave
  your machine. There is no telemetry and no account.
- **Recording other people is regulated.** Many US states and most of the EU
  require the consent of *all* parties. Laws vary by jurisdiction and this is
  not legal advice — know the rules where you are.
- **Voiceprints are biometric data.** The enrollment database identifies
  specific individuals. Treat it accordingly, and note that biometric data is
  specially regulated in many places.
- **`data/` is gitignored by default** and should stay that way. It will
  contain recordings and voiceprints of people who did not choose to be in
  your repository.
- **Build in a hardware mute.** A physical switch that cuts power to the
  microphone is the only mute anyone should have to trust.

---

## Naming voices

Diarization gives per-file labels (`SPEAKER_00`) that mean nothing across
recordings. Tapping a speaker chip attaches a name, and every later clip
resolves that voice on its own.

Enrolment is a running mean in embedding space, not training: no model is ever
retrained, and what improves is the reference vector each new voice is compared
against. It works from the first sample, sharpens with each label, and costs no
GPU time.

Because the reference is an average, a bad sample degrades it, so two checks
run before anything is enrolled:

- **Length.** Under 5 seconds of a voice in a clip is refused. Short speech
  makes an unreliable embedding, and averaging one in drags the reference away
  from how the person actually sounds.
- **Outliers.** A sample scoring below 0.55 against the existing reference is
  refused as probably belonging to somebody else. Measured on this hardware,
  the same speaker across recordings scores 0.65–0.87 and two different
  speakers 0.38–0.48, so 0.55 sits in the gap. An earlier threshold of 0.40 sat
  *inside* the different-speaker range and let a wrong voice enrol silently.

Both are advisory. The interface says why and offers to add it anyway, because
only the person listening can settle a genuinely unusual recording.

Every sample is kept individually rather than only the average, so the **People**
tab can list what a voiceprint was built from and remove any one of them, and the
reference is rebuilt without it.

Quality matters more than quantity. Measured on short real-world clips,
identification scores 0.42–0.74 against a 0.60 threshold — the same voice is
recognised in some clips and missed in others when the enrolment came from a
single session. Label long clips with clear speech, and use **split by voice**
first so the voiceprint comes from one person rather than a conversation.

## Custom words

Names, jargon and drug names are what a general transcriber gets wrong, so the
**People** tab takes a word list. Terms are applied **after** transcription:
near-misses are repaired, a single mangled word is matched fuzzily, and runs of
two or three words are re-joined — so *"she prescribed met form in"* comes back
as *"she prescribed Metformin"* and *"the boss well project"* as *"the Boswell
project"*.

> **Decode-time boosting is deliberately not used.** Passing the word list as
> `hotwords` or `initial_prompt` is the obvious approach and it loses audio.
> Measured on a 45-second recording: plain decoding produced two segments
> covering the whole clip; with boosting it produced one and silently dropped
> sixteen seconds of speech. Conditioning the decoder on a bare word list makes
> it treat some chunks as containing nothing. Correcting afterwards fixes the
> same mistakes and cannot make audio disappear.

Correction is deliberately conservative: only terms of five characters or more
are fuzzy-matched, and phrase merges need a closer match than single words.
*"He met Foreman at the office"* is left alone, which is the point — a wrong
correction is worse than a missed one.

Enrolled people are added to the list automatically, since their names are
exactly the words that get mangled.

Changing the list rebuilds the ASR model, which takes a few seconds, and applies
to clips transcribed from then on. Re-transcribe an older clip to apply it there.

## Power

Rough budget while streaming, on a device that has to run all day:

| | |
|---|---|
| BLE connection and notifies | ~5–8 mA |
| PDM microphone | ~1 mA |
| Status LED at full brightness | ~2 mA |
| IMU at 416 Hz (tap detection) | ~0.5 mA |

The LED being a quarter of the microphone-and-radio budget is why it is
controllable, and why **blink** beats **dim** — see below.

**The microphone is powered down when capture is disarmed.** Nothing is being
recorded then, and it is around 1 mA. It restarts, with its gain reapplied,
when capture is armed again.

**Battery voltage** is read through the board's 1M/510k divider against the
3.0 V internal reference, with `VBAT_ENABLE` driven low only for the reading.
Percentage comes from a breakpoint table rather than a linear map, because a
lithium discharge curve is flat through the middle and a linear reading would
be badly wrong for most of the cell's life.

> The voltage reading has only been exercised on USB power so far. Check it
> against a multimeter once a cell is attached and adjust `VBAT_DIVIDER` if it
> reads high or low.

**Charge current** defaults to 50 mA, which is a ten-hour charge for a 500 mAh
cell. The BQ25101's HICHG pin selects 100 mA, exposed as a switch. Only enable
it on a cell rated for that current.

## Status light

The LED is roughly 2 mA against a whole-device budget near 8 mA, so it is worth
controlling. Brightness is real PWM — average current tracks duty cycle almost
exactly, so 10% brightness costs about 10% of the current, and it is not burnt
off as heat.

There is a floor, though: the PWM peripheral itself draws roughly 50–100 µA, so
below a few percent it costs more than the light does. **Blink** mode exists for
that case — a 25 ms flash every 3 seconds is under 1% duty with no PWM running
at all, which beats any practical dim setting while still showing the device is
alive and what it is doing. Off costs nothing.

## The agent

Transcripts are reviewed by a local LLM without being asked, and whatever is
worth keeping is filed as a task, a calendar event, a durable fact or a note.
Everything lands in `data/agent/*.jsonl` and shows up under **Notes**.

**It waits for the conversation to end rather than firing per clip.** Clips are
30 seconds, which is half a sentence with no context; reasoning over that
produces sixty disconnected passes instead of one useful one. Transcripts
accumulate and the agent runs after 90 seconds of quiet — or after 15 minutes
regardless, for someone who does not stop talking.

Model defaults to `gpt-oss:20b`. It is not the strongest option available, but
it is ~13 GB and fits beside Whisper's ~9 GB on a 24 GB card; `glm-4.7-flash`
is the better MoE and at 19 GB the two cannot coexist. Any tool-capable Ollama
model can be selected from the Notes tab.

From a single 35-second test conversation it produced three tasks, one calendar
event and two facts, correctly separating "order a battery" from "order a
charger rated for 100 mA" and attributing each to the speaker who said it.

## Correcting transcripts

Tap any line to fix the words. Corrections are safe: voiceprints come from
audio embeddings and the waveform and split both key off timings, so none of
them care what the words say. The model's original wording is kept alongside
the correction, and the whole clip can be reverted.

The one thing that would destroy a correction is re-transcribing over it, so an
edited transcript is marked: bulk transcription skips it, and re-transcribing a
single clip returns 409 until you explicitly confirm.

Naming works at two levels, because misattribution has two different causes:

- **A speaker chip** names every line of that voice in the clip and enrols the
  voiceprint, so the person is recognised in later recordings. This is the one
  to use when a new person appears and has several lines.
- **A line's own name** applies to that line only and does not touch the
  voiceprint database, which is deliberate: an embedding describes a whole
  diarized cluster, so enrolling from one misattributed line would teach the
  wrong voice.

## Roadmap

- Opus encoding for lower power draw
- Step counting and activity detection — also hardware features of the
  LSM6DS3TR-C, at almost no CPU or code cost
- Rolling long-term memory across conversations
- Phone app to replace the laptop as the BLE host
- Enclosure and dock

---

## License

Apache-2.0. See [LICENSE](LICENSE).
