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

A small local service that owns the Bluetooth link and serves a responsive
front end — connect, arm and disarm capture, set microphone gain and VAD live,
watch the input level and the device's flash backlog, and save clips.

It is split deliberately so a phone app is a transport swap rather than a
rewrite: every piece of device state and every action is JSON over a single
WebSocket, and the browser holds no logic a native client could not
reimplement in a few dozen lines. The layout is mobile-first and reachable
from a phone on the same network.

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
