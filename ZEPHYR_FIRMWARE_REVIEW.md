# Zephyr Firmware Review

Scope: `firmware/zephyr/boswell/**`, with host/web contract references where the Zephyr firmware exposes data to them.

This is the active firmware direction and should be treated as the main implementation. The Arduino firmware is useful as historical reference and for protocol comparison, but Zephyr should own new behavior.

## Highest-Priority Issues

### 1. Info characteristic byte 38 is no longer populated

Files:

- `firmware/zephyr/boswell/src/ble_audio.c:367-411`
- `web/server.py:406-409`

The web server reads byte 38 as `ring_overruns`. Zephyr clears the 40-byte `info_buf` and never assigns byte 38, while byte 39 is used for `tx_power`. This means the web UI sees a valid-looking zero even when Zephyr has no equivalent value.

Recommendation: decide whether byte 38 remains part of the shared info contract. If yes, publish an equivalent Zephyr diagnostic. If no, change the host/UI to treat it as unavailable for Zephyr and document the byte as reserved.

### 2. QSPI staging ring concurrency needs proof or locking

File: `firmware/zephyr/boswell/src/qspi_store.c:253-269`, `301-314`

`qspi_store_push()` writes to the Zephyr `ring_buf` from the capture thread while the writer thread drains it. The space check plus two `ring_buf_put()` calls are not protected by the same lock used during reads.

Recommendation: either document the exact Zephyr single-producer/single-consumer safety guarantee being relied on, or protect staging ring access with a dedicated lock/critical section. Add malformed-record counters or a stress test for backlog pressure.

### 3. Conversation-time contract depends on Zephyr timestamps

Files:

- `firmware/zephyr/boswell/src/main.c:716-717`
- `web/index_db.py:235-253`

Zephyr correctly timestamps frames with `k_uptime_get_32()`. The web layer later uses device-time sidecar files to reconstruct conversation order. That makes firmware timestamp continuity a core data contract, not just a convenience.

Recommendation: document the timestamp wrap behavior and host expectation. At 32-bit milliseconds, uptime wraps after about 49.7 days; the host should either handle wrap or the firmware should expose a reboot/session marker in the info stream or clip metadata.

### 4. Codec assumes valid even nonzero sample counts

Files:

- `firmware/zephyr/boswell/src/codec.c:27-56`
- `firmware/zephyr/boswell/src/ima_adpcm.h:79-86`

Current callers pass 160 or 320 samples, so this is safe today. The codec itself assumes `count > 0` and even, reading `samples[0]` and then encoding pairs.

Recommendation: add `__ASSERT()` or explicit checks in `codec_build_frame()`. This makes future sample-rate/frame-size changes safer and catches contract violations close to the fault.

### 5. Watchdog coverage omits BLE and IMU by design, but the design is implicit

File: `firmware/zephyr/boswell/src/main.c:149-183`

The watchdog waits for main, capture, and QSPI. BLE and IMU are not included. That may be correct, but the criteria are not stated.

Recommendation: document why BLE and IMU are excluded. If BLE reachability is critical, consider adding a BLE health signal. If false resets are more risky than BLE stalls, say that explicitly.

## Reliability Recommendations

### Add a shared info-characteristic layout table

`proto.h` defines the audio frame and control opcodes well, but the info characteristic layout is implicit in publishers and host parsers.

Recommendation: create a table in `proto.h` or a dedicated `info_proto.h` defining bytes 0-39. Include codec, rate, flags, IMU fields, QSPI fields, LED fields, battery fields, diagnostics, and tx power.

### Make QSPI stats reads consistent

File: `firmware/zephyr/boswell/src/qspi_store.c:234-251`

`qspi_store_pending()`, `qspi_store_stats()`, and `qspi_store_dropped()` read shared writer/capture state without locking. For telemetry this may be acceptable, but it should be intentional.

Recommendation: either document these as approximate telemetry or lock/copy them consistently. For UI counters, approximate is fine; for host behavior decisions, consistency matters.

### Treat backlog mode as a protocol-level behavior

Files:

- `firmware/zephyr/boswell/src/main.c:43-46`, `722-738`
- `web/static/index.html:2252`

Zephyr defaults to backlog live-first mode. That is a meaningful product behavior because it changes whether recovered audio arrives interleaved or strictly ordered.

Recommendation: ensure the UI always reflects the device's actual backlog mode after connect. Consider persisting and displaying the mode as part of the info contract rather than only assuming host state.

### Add tests for frame and QSPI invariants

Good firmware tests here do not require hardware if they target pure logic:

- ADPCM frame build/decode roundtrip.
- Header flags and sample counts.
- QSPI record append/pop/resync over wrap.
- Timestamp monotonic behavior and wrap handling.

Recommendation: add a host-side C unit test or small native Zephyr test for codec and QSPI record logic. These are high-value because most regressions would be silent audio ordering/data loss bugs.

## Design Notes To Preserve

- The Zephyr thread split is the right direction: capture should not share a polling loop with BLE, IMU, flash, and UI concerns.
- The watchdog only feeds after multiple critical threads check in; preserve that pattern.
- Per-frame ADPCM state is the correct BLE-loss strategy.
- QSPI store-and-forward uses a magic byte and bounded record payloads, which is appropriate for circular flash recovery.
- The board overlay captures hardware discoveries that should not be "simplified away": mic regulator boot-on, IMU regulator always-on, real LED PWM pins, ADC configuration, and QSPI stack-write-buffer behavior.
- Comments in this firmware are operational knowledge. They document hardware failures and timing bugs that are not obvious from the final code.

## Suggested Zephyr Work Order

1. Define the info characteristic byte layout and resolve byte 38.
2. Prove or lock the QSPI staging ring concurrency.
3. Add codec assertions for sample count invariants.
4. Document timestamp wrap/session behavior.
5. Add QSPI/codec pure tests.
6. Make watchdog inclusion/exclusion criteria explicit.
