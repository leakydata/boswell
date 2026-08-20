# Zephyr Firmware Review

Reviewed: 2026-08-19 at commit `9e61072`

## Scope and Method

This is a fresh review of every authored line in `firmware/zephyr/boswell/**` and
`firmware/zephyr/build.sh`. Host code was also traced where it interprets the
Zephyr wire protocol, timestamps, IMU scale, and store-and-forward behavior.
Generated build output and the Zephyr/NCS SDK itself are outside scope.

Severity meanings:

- **Critical**: credible reset loop, silent loss of captured audio, or remote
  control exposure.
- **High**: incorrect user-visible behavior or a likely reliability failure.
- **Medium**: defensive correctness, maintainability, or observability gap.
- **Low**: cleanup that should follow the functional work.

## Executive Summary

The firmware builds successfully and has a sensible thread split, bounded audio
buffers, per-frame ADPCM state, and useful hardware diagnostics. The largest
remaining risks are concentrated in QSPI failure semantics, watchdog membership,
BLE sequence accounting, and cross-firmware protocol behavior. Several comments
currently claim guarantees that the implementation does not provide.

Before relying on Zephyr for unattended recording, address ZF-01 through ZF-07.

## Critical Findings

### ZF-01: Clearing the flash buffer does not clear queued RAM records

Evidence: `src/qspi_store.c:403-411`, called from `src/main.c:479`.

`qspi_store_reset()` resets flash cursors and `page_fill`, but it does not clear
the `stage` ring. Frames already queued in RAM can therefore be written back to
the freshly cleared logical buffer. Capture can also continue producing while
the reset runs, so simply adding `ring_buf_reset()` from the BLE thread would
violate the documented single-producer/single-consumer contract.

Recommendation: make clear-buffer a coordinated writer-thread operation. Pause
or epoch-tag producer input, have the writer drain/reset its own ring and page
state, reset cursors under the mutex, then resume. Return completion/failure to
the control path and publish a clear counter or generation in diagnostics.

### ZF-02: Flash write failures silently discard data already removed from RAM

Evidence: `src/qspi_store.c:320-333`, `168-190`, and `344-358`.

The writer removes bytes from `stage`, calls `flush_page()`, and discards its
return value. `qspi_store_pop()` also ignores a flush failure. On erase or write
failure the page remains in RAM, but the outer logic can append more data into
an already-full page or repeatedly operate on inconsistent state. There is no
fault state, retry policy, or exposed count.

Recommendation: do not consume additional staging bytes until the current page
commits. Retry transient failures with bounded backoff; after a limit, mark the
store degraded, preserve the pending page, increment a persistent error counter,
and let capture account for rejected frames explicitly.

### ZF-03: A replay frame is removed before BLE delivery succeeds

Evidence: `src/qspi_store.c:313-317`, `344-384` and `src/main.c:511-518`.

`qspi_store_pop()` advances `r_pos` before `drain_cb()` reports whether the BLE
notification was accepted. If notification fails, the loop stops but that frame
has already been forgotten. This converts temporary radio backpressure into
permanent recovered-audio loss.

Recommendation: implement peek/commit semantics. Read without advancing, notify,
and only commit the read cursor after success. Keep the current frame available
for retry across writer-loop iterations.

### ZF-04: Failed live notifications reuse the sequence number and hide loss

Evidence: `src/main.c:749-751` and `src/ble_audio.c:348-369`.

The sequence increments only when `ble_audio_send()` returns success. After all
20 notification retries fail, the next captured frame reuses the same sequence.
The host therefore cannot see the dropped frame. The comment in
`ble_audio.c:355-358` describes this exact failure, but the caller still does it.

Recommendation: assign and increment the capture sequence once per produced
frame, independent of transport success. Track notify drops separately. If the
product requirement is lossless behavior, enqueue a failed live frame to QSPI
instead of silently dropping it.

### ZF-05: A failed QSPI initialization creates a watchdog reboot loop

Evidence: `src/main.c:178-199`, `789-815` and `src/qspi_store.c:193-231`.

`WDT_ALL` always requires `WDT_QSPI`, but the only QSPI check-in runs in the
writer thread, which is created only after successful `qspi_store_init()`. If
flash probe or initialization fails, the application otherwise continues, then
the watchdog resets it every 30 seconds.

Recommendation: build the required watchdog mask from successfully initialized
subsystems. Alternatively, fail boot explicitly when QSPI is mandatory. Add a
test or fault-injection build where QSPI is absent.

### ZF-06: BLE control is unauthenticated

Evidence: `src/ble_audio.c:114-127` and control operations in
`src/main.c:465-500`.

The control characteristic is open. Any nearby BLE central can arm or disarm
capture, erase the backlog, change settings, enable high-current features, or
request DFU. This matters more for a wearable microphone than for ordinary
telemetry.

Recommendation: require an encrypted/authenticated connection for control and
CCC writes, enable bonding, and define a physical recovery/pairing flow. If open
BLE is intentional during development, compile it behind an explicit insecure
development option and document the threat model.

## High Findings

### ZF-07: `qspi_store_pending()` is neither an atomic snapshot nor telemetry-only

Evidence: `src/qspi_store.c:25-27`, `234-241` and `src/main.c:742-746`.

The 64-bit cursors are read lock-free on a 32-bit nRF52840 and can tear. The
comment says the result is only approximate screen telemetry and is not used for
decisions, but capture uses it to decide whether live audio must join the
backlog. A torn or incoherent value can route audio incorrectly and can publish
a nonsensical pending count.

Recommendation: expose a coherent locked snapshot, or maintain an atomic 32-bit
pending-byte counter with well-defined saturation. Keep exact control decisions
inside the QSPI owner thread.

### ZF-08: Buffered audio is abandoned on every reboot

Evidence: `src/qspi_store.c:219-223`.

Initialization resets all logical cursors to zero without scanning flash or
loading metadata. Audio physically present in QSPI cannot be replayed after a
watchdog reset, battery interruption, firmware crash, or normal reboot.

Recommendation: persist redundant, checksummed cursor metadata with a generation
number, or scan versioned records on boot. If reboot persistence is deliberately
out of scope, state that limitation prominently because it weakens the primary
store-and-forward guarantee.

### ZF-09: The record format cannot reliably distinguish corruption from data

Evidence: `src/qspi_store.c:361-395` and `src/qspi_store.h`.

Records contain only a one-byte magic value and length. After a torn write or
sector loss, any payload byte equal to the magic value followed by a plausible
length can be accepted as a frame. The host then receives plausible-looking but
corrupt ADPCM.

Recommendation: use a versioned record header containing payload length,
sequence, and CRC16/CRC32. Validate the embedded audio header before accepting a
resynchronized record.

### ZF-10: The 8 kHz decimator aliases speech-band energy

Evidence: `src/main.c:684-691`.

Pair averaging is a two-tap moving average. Its first null is at 8 kHz for a
16 kHz input, not at 4 kHz as the comment states. It attenuates the new 4 kHz
Nyquist point by only about 3 dB, so content above 4 kHz folds into the 8 kHz
output.

Recommendation: use a proper low-pass FIR decimator with enough stopband
attenuation before downsampling. Add a tone-sweep test that measures alias
rejection. Correct the matching Arduino comment and implementation together.

### ZF-11: Zephyr VAD has hangover but no pre-roll

Evidence: `src/main.c:696-715`.

The gate opens only after a frame crosses the threshold. Quiet consonants and
word onsets immediately before that frame are lost. Arduino already has a
three-frame pre-roll design, although its timestamps need correction.

Recommendation: retain approximately 60-100 ms of encoded or PCM frames and
flush them oldest-first when the gate opens. Preserve original sequence numbers
and timestamps.

### ZF-12: Backlog mode does not mean the same thing across firmware and UI

Evidence: `src/main.c:476`, `696-746`; compare Arduino `ble_mic.ino:147-150` and
`780-852`.

In Zephyr, mode 0 disables disconnected buffering and mode 1 enforces strict
queue ordering. In Arduino, both policies buffer and mode 1 sends live audio
while trickling replay. The host presents this as a shared policy control. A
single bit currently conflates "buffer enabled" with "replay policy."

Recommendation: define separate protocol fields for store-and-forward enabled
and replay policy (`strict`, `live-first`, possibly `paused`). Version the change
and add contract tests against both firmware implementations.

### ZF-13: Restored settings are trusted without range validation

Evidence: `src/main.c:561-578` and `src/cfg_store.c`.

Magic/version validation does not validate gain, boolean fields, VAD threshold,
LED mode/level, backlog mode, tap threshold/debounce, or supported TX power.
Corruption or a future layout mistake can create invalid state. For example,
`backlog_mode << 1` in `src/ble_audio.c:390-392` can collide with the capture bit
if the restored value is greater than one.

Recommendation: validate and clamp every persisted field at the load boundary,
reject impossible combinations, and include an explicit migration path for each
settings version.

### ZF-14: TX power changes only the advertising handle

Evidence: `src/main.c:433-460`.

The vendor HCI command uses `BT_HCI_VS_LL_HANDLE_TYPE_ADV` and handle zero. This
does not establish that an active connection uses the requested power, while the
UI labels the control as radio transmit power.

Recommendation: apply power to the advertising set and the current connection
handle, reapply on connect, and publish the accepted controller value rather than
only the requested value.

### ZF-15: IMU gyro scaling disagrees with the host

Evidence: `src/imu_tap.c:296-304` configures 500 dps, while
`host/imu_capture.py:36-38` converts as 2000 dps.

Gyroscope CSV values are therefore scaled by a factor of four. This is a
cross-layer correctness bug, not a cosmetic label.

Recommendation: put sensor range in the IMU frame or capability metadata and
derive host scaling from it. At minimum, change the host constant and add a
known-rate rotation test.

## Medium Findings

### ZF-16: Codec boundaries rely on undocumented caller invariants

Evidence: `src/codec.c:33-55` and `src/ima_adpcm.h:75-82`.

The encoder dereferences `samples[0]` and reads pairs without checking null
pointers, positive count, even count, or output capacity.

Recommendation: return an error or use `__ASSERT()` for these invariants and add
unit tests for zero, odd, and maximum counts.

### ZF-17: Device timestamps wrap and do not identify a boot session

Evidence: `src/main.c:723-724`.

`k_uptime_get_32()` wraps after about 49.7 days. Reboots also restart it at zero.
The host uses this value to place recovered clips but has no boot/session ID and
currently compares timestamps with ordinary min/max operations.

Recommendation: publish a random boot ID or persistent session counter and use
wrap-aware arithmetic. Carry that identity into clip sidecars.

### ZF-18: IMU frame collection can run indefinitely after sensor failures

Evidence: `src/main.c:609-623`.

The loop stops only after collecting ten successful reads. Repeated I2C failures
produce an endless sleep/retry loop and no frame or error transition.

Recommendation: bound the frame by elapsed time or attempts, report partial
frames explicitly, and re-probe/reinitialize after a failure threshold.

### ZF-19: Several hardware failures are ignored or flattened

Evidence: `src/mic.c:120-123`, GPIO setup in `src/battery.c`, and register/setup
calls throughout `src/imu_tap.c`.

DMIC read failures all become zero samples, and multiple GPIO/I2C return values
are ignored. This prevents distinguishing timeout, overrun, disconnected sensor,
and configuration failure.

Recommendation: retain error codes, expose counters in diagnostics, and perform
bounded subsystem recovery. Fail initialization when required pin or callback
configuration does not succeed.

### ZF-20: GATT attribute array indices are brittle

Evidence: `src/ble_audio.c:327-340`, `348-362`.

Audio and IMU notifications use hard-coded service indices 1 and 8. Adding or
reordering an attribute can silently send on the wrong characteristic.

Recommendation: use named value-attribute pointers/macros or compile-time
assertions tied to the service declaration.

### ZF-21: Reading info consumes motion events

Evidence: `src/ble_audio.c:405-414` and `src/imu_tap.c:331-342`.

Publishing/reading info clears tilt and significant-motion latches. A diagnostic
read therefore changes application state, and multiple readers can steal events
from each other.

Recommendation: separate peek from acknowledge, or define info reads as an event
consumer and document the single-consumer contract.

### ZF-22: Shared state is not synchronized as a coherent snapshot

Evidence: `g_state` access across capture, IMU, Bluetooth, and main threads;
connection/CCC flags in `src/ble_audio.c`.

Aligned byte accesses are naturally atomic on this MCU, but groups of fields can
be observed from different moments and the C memory model does not make ordinary
cross-thread access a synchronization mechanism.

Recommendation: use Zephyr atomics for flags and a mutex or versioned snapshot
for compound state. Keep callbacks short by applying settings through a message
queue when hardware operations may block.

### ZF-23: Pure firmware behavior has no native tests

The Python tests validate host-side ordering but do not execute Zephyr codec,
VAD, settings validation, or QSPI state transitions.

Recommendation: add Zephyr `ztest` coverage for frame encoding, timestamp/sequence
rules, decimation response, VAD pre-roll, record CRC/resync, cursor wrap,
clear-buffer coordination, flash write failure, and watchdog degraded mode.

## Build and Tooling Notes

- `west build` completed successfully for `xiao_ble/nrf52840/sense` using NCS
  Zephyr 3.7.99 and SDK 0.17.0.
- Image usage: 251,528 bytes flash (31.17%) and 118,220 bytes RAM (45.10%).
- The first sandboxed build attempt failed because Zephyr writes its compiler
  capability cache under the SDK checkout. Supplying
  `-DUSER_CACHE_DIR=/tmp/boswell-zephyr-cache` made the build reproducible in a
  restricted environment. Consider exposing this as a build-script option.
- `host/flash_zephyr.sh:15-17` checks "newer than five minutes," not newer than
  the actual build start. Record a timestamp before building and compare against
  that exact marker so a recent stale artifact cannot pass.

## Strengths to Preserve

- Audio capture, BLE, QSPI, IMU, and housekeeping are separated by responsibility.
- The capture thread never blocks on a flash erase.
- ADPCM state is reset per frame, limiting packet-loss damage.
- The device publishes firmware identity and capability bits.
- Settings are applied after driver initialization in the Zephyr path.
- The board overlay records important regulator, mic, ADC, LED, and QSPI facts.
- Existing comments document real hardware failures and should remain, but they
  must be kept aligned with behavior.

## Recommended Work Order

1. Make QSPI clear, write failure, and replay delivery transactional (ZF-01 to ZF-03).
2. Fix sequence accounting and conditional watchdog membership (ZF-04, ZF-05).
3. Secure BLE control before field use (ZF-06).
4. Make pending/cursor state coherent and reboot-recoverable (ZF-07 to ZF-09).
5. Correct decimation, VAD pre-roll, and backlog protocol semantics (ZF-10 to ZF-12).
6. Validate settings, TX power behavior, and IMU scaling (ZF-13 to ZF-15).
7. Add native fault-injection and protocol tests.

## Files Reviewed

- `firmware/zephyr/boswell/CMakeLists.txt`
- `firmware/zephyr/boswell/prj.conf`
- `firmware/zephyr/boswell/boards/xiao_ble_nrf52840_sense.overlay`
- Every `.c` and `.h` file under `firmware/zephyr/boswell/src/`
- `firmware/zephyr/build.sh`
- Contract references in `web/server.py`, `host/imu_capture.py`,
  `host/ble_capture.py`, `host/relay.py`, and flashing scripts
