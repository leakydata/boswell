# Standalone capture — status

Working notes for the plan at
`https://claude.ai/code/artifact/2c637ea6-c419-4971-acd9-bc848c1dc113`.
What is built, and what is waiting on a decision or on hardware.

## Built

| | what | verified |
|---|---|---|
| Opus | vendored, encoder wired, host decodes either codec | 4 tests; never run on a board |
| Card | mounts, round-trips a file, reports capacity | **measured on hardware**: 30535680 sectors, 14893 MB |
| Device selection | `BOSWELL_DEVICE` picks a board by address or name | 10 tests |
| De-duplication | `web/dedup.py`, `boot_id` in every times record | 18 tests; not wired to an ingest path |
| Tap controls | enable + threshold from the app | 6 tests |
| Card status | presence and free space in the UI | 7 tests |

## Waiting on a decision

**The capture path onto the card cannot be written yet, because writing it
chooses the storage format — and the format is the fork.**

The two branches are not just different ways to move audio off the card;
they are different things on it:

- **FAT32 + USB mass storage.** Audio lands as files. Dock the device and
  the host reads them at about 1 MB/s — a day in eleven minutes, with almost
  no host-side work.
- **Raw sectors + BLE transfer.** No filesystem to corrupt on power loss, no
  allocation cost, even wear. This is what Omi's production firmware does,
  and it rules out mounting the card as a disk, so every byte has to come
  off over the radio.

There is no format that keeps both options open: a raw-sector ring is not
mountable, and a FAT volume gives up the power-loss guarantee that is the
reason to choose raw. Picking one is Nathan's call, and everything after it —
retention, offload, ingest — follows from it.

What is *not* blocked, and is already done, is the part common to both: the
card mounts and reports itself, and `web/dedup.py` answers "do I already
have this audio" from `(boot_id, device_ms)` regardless of how it arrived.

## Waiting on hardware

**The Opus encoder's reserve has not been measured.** `codec.c` sets aside
20 KB for the encoder state and checks at init that the real figure fits.
The real figure is a runtime property of how the library was configured and
cannot be known at compile time, so it has never been compared against the
reserve.

`boswell opus` now reports both. Run it after the next flash:

```
boswell opus
opus: encoder needs N bytes, reserved 20480 (M spare)
```

Then `enc_mem` can be trimmed to N plus a margin. It is deliberately *not*
trimmed on a calculated guess: too small means `opus_encoder_init` fails,
and a codec that fails to initialise presents as a silent capture stop,
which is the failure this project keeps finding in other forms.

**Nothing Opus-related has run on a board.** The encoder builds and the host
decodes what libopus produces, but no audio has made the trip. First flash
should be the combined build, then listen to a clip before trusting it.

## Building the variants

```bash
firmware/zephyr/build.sh                              # wearable, no card, ADPCM

EXTRA_OVERLAY=boards/audio_bff.overlay \
EXTRA_CONF="sdcard.conf;opus.conf" \
BUILD_DIR=/tmp/boswell-full-build \
  firmware/zephyr/build.sh                            # Audio BFF, card + Opus
```

The plain image must stay 563,712 bytes — that is the check that none of
this leaked into the build the wearable runs.
