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

**Measured, and trimmed.** `boswell opus` on the board reports the encoder
wants **7,180 bytes** against the 20 KB that had been set aside — 2.9 times
larger than needed. `enc_mem` is now 8 KB, leaving about a kilobyte of
headroom and handing back twelve. RAM on the combined build went 60.63% to
55.94%.

The check at init is what makes tightening it safe: a build configured
differently — SILK, or stereo — would want far more, and would say so at
init rather than running off the end of the array.

**No audio has been through Opus on a board.** The encoder initialises on
hardware and the host decodes what libopus produces, but nothing has been
captured, encoded, transmitted and played back end to end. That needs
capture armed, and it is the one thing still unproven.

## Building the variants

```bash
firmware/zephyr/build.sh                              # wearable, no card, ADPCM

EXTRA_OVERLAY=boards/audio_bff.overlay \
EXTRA_CONF="sdcard.conf;opus.conf" \
BUILD_DIR=/tmp/boswell-full-build \
  firmware/zephyr/build.sh                            # Audio BFF, card + Opus
```

The plain image size is the check that none of the card or codec work leaked
into the build the wearable runs. The baseline is **564,224 bytes** as of
`boswell drop`.

It moved once, deliberately, from 563,712: the backlog-clear command is not
card code or codec code, it is a general diagnostic that a wearable with a
stuck backlog needs just as much, so it belongs in both builds and costs 512
bytes there. Every other change to this number has meant something leaked and
should be treated that way.
