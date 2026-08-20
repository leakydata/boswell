# Arduino Firmware Review

Reviewed: 2026-08-19 at commit `9e61072`

## Scope and Role

This is a fresh line-by-line review of:

- `firmware/ble_mic/ble_mic.ino`
- `firmware/ble_mic/ima_adpcm.h`
- `firmware/ble_mic/imu_tap.h`
- `firmware/ble_mic/qspi_store.h`
- `firmware/ble_mic/settings.h`
- `firmware/mic_usb_pcm/mic_usb_pcm.ino`

Arduino is treated as legacy/reference firmware, as requested. Issues that can
corrupt shared host behavior or make the reference unusable are still ranked
highly; feature parity work can remain behind Zephyr.

## Executive Summary

Both Arduino sketches compile, but the BLE firmware has a release-blocking
watchdog defect: it starts a 30-second hardware watchdog and never feeds it. The
web service also misreads Arduino's capture state and can issue a stream-start
command every second, which clears pending microphone samples each time.

If Arduino is retained only as hardware documentation, fix AF-01 and AF-02 and
label the build legacy. If it is expected to record reliably, address AF-01
through AF-12.

## Critical Findings

### AF-01: The board resets every 30 seconds because the watchdog is never fed

Evidence: `firmware/ble_mic/ble_mic.ino:611-624`, `680`, and the complete
absence of any `watchdogFeed()` call.

`watchdogBegin()` starts the nRF hardware watchdog. Once started it cannot be
stopped by application code, and no loop/setup path reloads RR0. The firmware
therefore enters a roughly 30-second reset cycle. The comment says a watchdog
turns a hang into a two-second gap, while the configured timeout is 30 seconds.

Recommendation: feed only after required loop responsibilities have completed,
or remove the watchdog until a meaningful health model exists. If QSPI/IMU setup
can be slow, start it after setup or feed from bounded setup stages. Add a
hardware soak test that proves uptime exceeds several watchdog periods.

### AF-02: Arduino omits capture-state info, causing repeated ring flushes from web

Evidence:

- `firmware/ble_mic/ble_mic.ino:364-418` publishes VAD/backlog in byte 5 but
  never sets capture-running bit 2.
- `web/server.py:371-383` interprets bit 2 as actual capture state.
- `web/server.py:351-363` re-sends start when the bit is absent.
- Arduino handles every start by resetting `ringTail=ringHead` at
  `ble_mic.ino:426-435`.

While connected to the web service, Arduino can discard all samples waiting in
the ring approximately once per second. This can create recurring gaps without
an obvious firmware error.

Recommendation: set bit 2 from `streaming` and add a capability bit indicating
that capture-state feedback is valid. The host must gate re-arm logic on that
capability so older firmware remains safe. Test both Arduino and Zephyr info
layouts through the real host parser.

### AF-03: Connection is mistaken for notification readiness

Evidence: `firmware/ble_mic/ble_mic.ino:498-505`, `551-557`.

`connected` becomes true immediately on GAP connect, before the central enables
the audio CCC. `emitFrame()` then attempts notifications and does not fall back
to QSPI when `notify()` fails. A central that connects without subscribing, or
a temporarily full notification queue, causes live audio loss and suppresses
store-and-forward.

Recommendation: track CCC subscription separately from GAP connection. Route to
flash unless notification is enabled and the send succeeds; keep an explicit
notify-drop counter and disconnect stale never-subscribed centrals.

### AF-04: BLE control is open to any nearby central

Evidence: `firmware/ble_mic/ble_mic.ino:651-667`.

Control writes use `SECMODE_OPEN`. Nearby devices can arm/disarm recording,
clear buffered audio, change power/settings, and affect behavior.

Recommendation: require authenticated/encrypted control and bonding, with a
physical pairing/recovery action. If Arduino remains development-only, make the
insecure mode explicit at compile time.

## High Findings

### AF-05: Restored settings do not reach already-initialized hardware

Evidence: hardware is configured at `ble_mic.ino:634-680`, then settings are
loaded at `683-702`.

Restored gain, TX power, LED level/mode, and related variables are assigned after
PDM, Bluefruit, and LED initialization. The variables report saved values, but
hardware retains defaults until a later control operation. Fast-charge is loaded
again separately, which makes the ordering harder to reason about.

Recommendation: load and validate settings before hardware setup where possible,
then apply every accepted value once after each driver is ready. Publish info
only after this application step.

### AF-06: Strict backlog mode deliberately throws away all newly captured audio

Evidence: `firmware/ble_mic/ble_mic.ino:780-791`.

The comment says live audio waits while backlog drains, but the implementation
sets `ringTail=ringHead` and returns. Current conversation audio is permanently
discarded. This conflicts with the store-and-forward goal and differs from
Zephyr, which queues current frames behind the backlog.

Recommendation: either queue live frames behind replay in strict mode or stop
capture intentionally and tell the user that recording is paused. Define
buffer-enabled and replay-order policy as separate protocol fields.

### AF-07: QSPI payload reads do not handle physical flash wrap

Evidence: `firmware/ble_mic/qspi_store.h:133-148`.

The header is read byte-by-byte with modular addressing, but payload uses one
`readBuffer((qspiRead + 2) % capacity, ..., len)`. A record whose payload starts
near the end of flash and crosses address zero can fail or read outside the
logical device range.

Recommendation: split reads at the capacity boundary, as the Zephyr
`read_wrapped()` helper does. Add a test record with its header at the final
flash byte and payload spanning address zero.

### AF-08: Every QSPI erase/read/write result is ignored

Evidence: `firmware/ble_mic/qspi_store.h:86-99`, `127-155`.

The store advances counters and cursors even if erase, page program, or read
fails. It can then report queued audio that was never stored, or consume data it
never read.

Recommendation: propagate operation status, retry bounded transient failures,
advance state only after success, and publish store error/drop counters.

### AF-09: QSPI data cannot survive reboot and lacks integrity checks

Evidence: `firmware/ble_mic/qspi_store.h:68-80`, `109-155`.

`qspiBegin()` resets all cursors every boot. Records contain only magic and
length, so false resynchronization is possible after torn writes or corruption.

Recommendation: if Arduino store-and-forward remains supported, persist
redundant cursor metadata and use a versioned record with sequence and CRC. If
Arduino is only reference firmware, document that reboot loses the backlog.

### AF-10: The 8 kHz downsampler aliases high-frequency content

Evidence: `firmware/ble_mic/ble_mic.ino:802-818`.

Pair averaging has its first null at 8 kHz for a 16 kHz source, not at 4 kHz as
the comment states. At the new Nyquist frequency it provides only about 3 dB of
attenuation, so frequencies above 4 kHz fold into the output.

Recommendation: use the same tested FIR decimator selected for Zephyr and verify
alias rejection with generated tones.

### AF-11: Pre-roll timestamps are recorded and then discarded

Evidence: `firmware/ble_mic/ble_mic.ino:583-605`.

`stashPreRoll()` saves `preRollStamp`, but `flushPreRoll()` calls
`emitFrame()`, which overwrites `frameStampMs` with the current `millis()`.
All pre-roll frames are dated at flush time instead of capture time.

Recommendation: pass timestamp explicitly into frame construction and use the
stored timestamp for each pre-roll frame. Remove the unused implicit global
timestamp contract.

### AF-12: The shared info layout is internally contradictory

Evidence: `firmware/ble_mic/ble_mic.ino:381-407`.

The code writes six IMU readback bytes to info 18-23, then overwrites bytes 18-21
with layout version, firmware ID, and capabilities. Bytes 22-23 remain stale
fragments of the old readback layout while comments call 18-21 free. Host
`tap_test.py:39-48` still interprets 18-23 as six registers, so its diagnostics
are now wrong.

Recommendation: define one versioned layout table. Stop writing the obsolete
fields, reserve bytes deterministically, update diagnostic tools, and test every
published byte.

## Medium Findings

### AF-13: Settings replacement is not power-loss safe

Evidence: `firmware/ble_mic/ble_mic.ino:262-272`.

The old settings file is removed before the replacement is written. A reset,
full filesystem, or write failure in between loses valid settings, and write
length/result is not checked.

Recommendation: use a dual-slot record with generation and CRC, or a verified
temporary file plus atomic rename if LittleFS exposes that safely.

### AF-14: Persisted settings are not range-validated

Evidence: settings load at `ble_mic.ino:683-702` and `settings.h`.

Magic/version alone does not prove gain, booleans, LED mode, backlog mode, VAD
threshold, TX power, or tap threshold are valid.

Recommendation: validate/clamp every field and reject impossible combinations
before assigning runtime state.

### AF-15: PDM restart success is ignored

Evidence: `firmware/ble_mic/ble_mic.ino:769-777`.

The code sets `micRunning=true` even if `PDM.begin()` fails after power-save.
The system can then report an active microphone while no samples arrive.

Recommendation: check the return, retry with a limit, publish a mic fault, and
leave `micRunning=false` until initialization succeeds.

### AF-16: Ring-overrun behavior and telemetry do not match the comment

Evidence: `firmware/ble_mic/ble_mic.ino:235-246`.

The comment says the oldest sample is dropped, but the callback breaks and drops
the remainder of the newest PDM chunk. `ringOverruns` increments once per
callback event rather than by dropped samples, then saturates to eight bits in
info.

Recommendation: choose and implement an explicit overflow policy. Count dropped
samples and events separately, use a wider diagnostic field, and avoid resetting
the only lifetime evidence on every stream command.

### AF-17: Protocol constants are duplicated manually

Evidence: `firmware/ble_mic/ble_mic.ino:50-61` versus Zephyr
`firmware/zephyr/boswell/src/proto.h`.

UUIDs, opcodes, flags, info offsets, capabilities, and frame layout are not
generated from one definition. AF-02 and AF-12 demonstrate the resulting drift.

Recommendation: generate Arduino C++ and Zephyr C headers plus host constants
from one small schema, or add a build-time comparison test if generation is too
heavy.

### AF-18: IMU setup ignores important register-write failures

Evidence: configuration and runtime setters throughout
`firmware/ble_mic/imu_tap.h`, especially initialization near `211-223`.

The sensor can be marked ready even when only some tap/motion registers were
accepted. Runtime threshold writes also need to preserve every required
non-threshold bit intentionally.

Recommendation: check each write/readback, fail or degrade capabilities when
configuration is incomplete, and expose the exact failed register.

### AF-19: Frame encoder trusts positive even sample counts

Evidence: `firmware/ble_mic/ble_mic.ino:522-548` and
`firmware/ble_mic/ima_adpcm.h`.

It dereferences sample zero and encodes pairs without boundary assertions.

Recommendation: add assertions or explicit validation, kept identical to the
Zephyr codec contract.

### AF-20: BLE notify failures need sequence/drop accounting

Evidence: `firmware/ble_mic/ble_mic.ino:551-580`, `847-852`.

Live notify results are ignored while the sequence still advances. The host can
observe a gap, but firmware exposes no reason and does not retain the frame.
Replay keeps one failed frame in `drainBuf`, which is better, but its QSPI read
cursor has already advanced and a reset would lose it.

Recommendation: count transport drops, route failed live sends to flash when
possible, and use durable peek/commit semantics for replay.

## USB PCM Sketch Findings

### AF-21: Microphone gain is set before `PDM.begin()`

Evidence: `firmware/mic_usb_pcm/mic_usb_pcm.ino:55-63`.

The BLE firmware correctly documents that `PDM.begin()` resets gain. The USB
sketch sets gain first, so its configured `MIC_GAIN` may not take effect.

Recommendation: call `PDM.setGain(MIC_GAIN)` only after successful begin.

### AF-22: The PDM callback can overwrite audio during USB transmission

Evidence: `firmware/mic_usb_pcm/mic_usb_pcm.ino:35-45`, `66-94`.

The loop disables interrupts only while copying metadata, then re-enables them
before `Serial.write(sampleBuffer, ...)`. A new callback can overwrite the same
buffer during the write. `drops` may increase, but the emitted frame itself can
contain torn audio.

Recommendation: use double buffering or a ring. Transfer ownership of a complete
buffer atomically and do not return it to the callback until USB finishes.

### AF-23: PDM input length is not bounded by the sample buffer

Evidence: `firmware/mic_usb_pcm/mic_usb_pcm.ino:35-45`.

`bytesAvailable` is passed directly to `PDM.read()` without clamping to
`sizeof(sampleBuffer)`.

Recommendation: clamp, count excess bytes, and make the framing receiver reject
impossible sample counts.

### AF-24: Setup blocks forever without a USB serial host

Evidence: `firmware/mic_usb_pcm/mic_usb_pcm.ino:48-50`.

This may be acceptable for a USB-only diagnostic sketch, but it prevents
headless capture and can look like a dead board.

Recommendation: add a timeout or state explicitly that host enumeration is a
required precondition.

## Build Results

- BLE firmware compiled successfully with
  `Seeeduino:nrf52:xiaonRF52840Sense`.
- BLE image: 132,516 bytes flash (16%); 51,496 bytes global RAM (21%).
- USB PCM firmware compiled successfully with
  `Seeeduino:mbed:xiaonRF52840Sense`.
- USB image: 85,184 bytes flash (10%); 45,096 bytes global RAM (18%).
- Compilation confirms syntax/link compatibility only. No board-level watchdog,
  BLE subscription, QSPI wrap, audio quality, tap, battery, or long-duration
  behavior was exercised.

## Strengths Worth Preserving

- The Arduino comments contain valuable board-specific history about PDM gain,
  sensor power, flash timing, LED polarity, and battery measurement.
- Per-frame ADPCM state limits damage from packet loss.
- VAD hangover and pre-roll are the right general shape.
- Replay retains a notification-failed frame in RAM for retry.
- The large microphone ring recognizes worst-case NOR erase latency.

## Recommended Work Order

1. Fix or disable the watchdog (AF-01).
2. Repair the capture-state protocol and subscription-aware send path (AF-02, AF-03).
3. Apply restored settings and secure BLE control (AF-04, AF-05).
4. Decide whether strict replay may discard current audio (AF-06).
5. Make QSPI wrap, errors, integrity, and persistence explicit (AF-07 to AF-09).
6. Share decimation/protocol behavior with Zephyr (AF-10 to AF-12, AF-17).
7. Correct the USB diagnostic sketch if it remains part of hardware bring-up.

If Arduino is retired, preserve this report and the hardware comments, fix the
watchdog so accidental use is not misleading, and direct all supported recording
work to Zephyr.
