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

## The watchdog resets — cause, and what was not done

The board reset roughly every forty seconds with `last reset=0x00000002
watchdog`. Cause found, board stable, exposure not removed.

**What it was.** `persist_cursors()` in `qspi_store.c` writes the backlog
cursors to *internal* flash every `CURSOR_SAVE_MS` (60 s), and only while
`(w_pos - r_pos) > 0`. Internal flash writes have to be scheduled around the
radio by MPSL, and the board was connected at a 15 ms interval, which leaves
very little room to grant a timeslot. A write that cannot get one waits — and
if it waits past the 30 s watchdog window, `writer_fn` is blocked *inside* the
save, so `WDT_QSPI` never checks in again and the SoC resets.

**Evidence.** Cleared the backlog with `boswell drop` (457,472 B → 0). Then:

| build | observed | reboots |
|---|---|---|
| Opus only | 11 minutes | **0** |
| Card + Opus | 31 minutes | **0** |

`wake` climbed linearly throughout both, 50/s, exactly the writer's 20 ms
tick. Before clearing, the same board went down inside forty seconds.

**Two things that were believed and are false.** A missing watchdog check-in
was never the cause — all four were read and are sound (`WDT_MAIN` 500 ms,
`WDT_TX` 500 ms, `WDT_CAPTURE` 50 ms idle, `WDT_QSPI` 20 ms). And the card
does not do a slow FAT walk: with the card formatted, boot logs `mount:
mounted in 7 ms` and `stat: took 0 ms`, because FATFS reads the cached free
count from the FSINFO sector. The sixteen seconds seen once was the first
mount formatting a blank card — which is also why `boswell sd` hung then and
does not now.

**What was deliberately not changed.** The exposure remains: a backlog plus a
tight connection interval can still put an unbounded flash write on the
writer thread. Four options were weighed.

- *Raise the watchdog window.* Rejected. It masks genuine wedges, doubles how
  long a truly dead device stays dead, and the block can exceed any window.
- *Move the cursor save to its own thread.* The right fix, and the one to
  make. The writer would keep checking in while a slow save blocks
  harmlessly, and bookkeeping does not belong on the audio path anyway.
- *Save only when the radio is idle.* Rejected. MPSL does not usefully expose
  that, and a device that is always connected would never save at all.
- *Accept it, understood and logged.* Where this stands tonight.

The reason the right fix is not in this commit: it touches crash-recovery
correctness, and neither it nor the bug it fixes can be tested without a
backlog — which needs capture armed. Shipping an untested change to the path
that decides whether buffered audio survives a reset is a worse trade than
leaving a known, instrumented exposure in place. `boswell drop` recovers a
board that hits it, and a slow save now warns in the log.

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
into the build the wearable runs. The baseline is **564,736 bytes** as of
`boswell stacks`.

It has moved twice, deliberately. From 564,224 for `boswell stacks`, a
thread high-water report that any build benefits from. And before that, from 563,712: the backlog-clear command is not
card code or codec code, it is a general diagnostic that a wearable with a
stuck backlog needs just as much, so it belongs in both builds and costs 512
bytes there. Every other change to this number has meant something leaked and
should be treated that way.
