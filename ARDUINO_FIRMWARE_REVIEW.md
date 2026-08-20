# Arduino Firmware Review

Scope: `firmware/ble_mic/**` and `firmware/mic_usb_pcm/**`.

Context: this is the original firmware path and is no longer the main direction. Treat it as legacy/reference unless you need it for hardware bring-up, regression comparison, or compatibility with host tools.

## Role Going Forward

The Arduino firmware is still valuable for three reasons:

- It records hard-won board bring-up knowledge, especially IMU power, bit-banged I2C, PDM gain reset, QSPI part declaration, LED behavior, and battery pins.
- It is a reference implementation of the BLE audio protocol.
- It gives a simpler fallback firmware when the Zephyr/NCS toolchain is too heavy.

New feature work should happen in Zephyr unless there is a specific reason to preserve Arduino parity.

## Compatibility Issues To Track

### 1. Backlog mode defaults differ from Zephyr

Files:

- `firmware/ble_mic/ble_mic.ino:137`
- `firmware/zephyr/boswell/src/main.c:45`

Arduino defaults `backlogMode = 0`; Zephyr defaults `backlog_mode = 1`. That changes user-visible recovery behavior after reconnect.

Recommendation: either align defaults or document this in a firmware compatibility matrix. Since the host is supposed to work with either firmware, the device should publish the actual mode and the UI should trust that.

### 2. Info characteristic layouts are similar but not identical

Files:

- `firmware/ble_mic/ble_mic.ino:354-393`
- `firmware/zephyr/boswell/src/ble_audio.c:367-411`

Arduino publishes legacy tap diagnostics/readback fields in bytes 13-26 and ring overruns at byte 38. Zephyr uses bytes 13-17 for steps/motion and currently leaves byte 38 unset.

Recommendation: create an explicit info-characteristic version or capabilities byte. Without that, host-side code cannot safely know whether byte 13 means tap diagnostics or step count.

### 3. Arduino and Zephyr should share documentation, not necessarily code

The project already shares ADPCM and protocol concepts, but Arduino-specific constraints are different enough that trying to keep full code parity may waste time.

Recommendation: keep Arduino as a documented protocol/hardware reference. Keep only the compatibility pieces aligned: UUIDs, audio frame format, control opcodes, and enough info fields for the host to identify capabilities.

## Arduino-Specific Risks

### Ring buffer concurrency is intentionally low-level

File: `firmware/ble_mic/ble_mic.ino:214-233`, `745-789`

The PDM callback writes `ringHead` and `ring`, while `loop()` reads `ringTail` and drains samples. This is normal for Arduino-style firmware, but it is fragile compared with Zephyr's slab/thread model.

Recommendation: avoid expanding this path with new features. If kept, document which variables are ISR-owned vs loop-owned and keep the callback minimal.

### Settings writes remove then rewrite

File: `firmware/ble_mic/ble_mic.ino:250-264`

`settingsSave()` removes the existing config file before writing the new one. If power fails between remove and write, settings fall back to defaults.

Recommendation: acceptable for legacy firmware, but if Arduino remains supported, switch to temp-write then rename or keep a backup record.

### `sendFrame()` and `emitFrame()` timestamp behavior should be double-checked

Files:

- `firmware/ble_mic/ble_mic.ino:481-490`
- `firmware/ble_mic/ble_mic.ino:518-524`

`sendFrame()` builds with the current `frameStampMs`, while `emitFrame()` first sets `frameStampMs = millis()`. `flushPreRoll()` calls `emitFrame()` even though it stores `preRollStamp[]`, so pre-roll frames may not preserve their original capture timestamp.

Recommendation: if Arduino remains in use, verify pre-roll timestamps. For Zephyr, prefer keeping timestamp handling there and avoid backport work unless needed.

## Design Notes Worth Preserving In Zephyr

- The comments around PDM gain reset are important: `PDM.begin()` resets gain.
- The IMU high-drive P1.08 discovery is critical board knowledge.
- The bit-banged I2C rationale explains why Zephyr's bounded I2C driver is preferable.
- QSPI records with magic byte and bounded length are the right storage shape.
- The battery divider/charge-current pin details should remain documented.

## Suggested Arduino Work Order

Only do these if Arduino support remains important:

1. Add an info version/capability byte.
2. Align or document backlog default difference.
3. Escape hatch: keep Arduino build scripts working as a fallback.
4. Fix settings save atomicity.
5. Verify pre-roll timestamps.

Otherwise, leave Arduino stable and invest in Zephyr tests, protocol docs, and host compatibility.
