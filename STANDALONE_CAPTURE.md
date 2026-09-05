# Standalone capture — status

Working notes for the plan at
`https://claude.ai/code/artifact/2c637ea6-c419-4971-acd9-bc848c1dc113`.
What is built, and what is waiting on a decision or on hardware.

## Built

| | what | verified |
|---|---|---|
| Opus | vendored, encoder wired, host decodes either codec | **on hardware**: captured, encoded, transcribed; 11 min clean |
| Card | mounts, round-trips a file, reports capacity | **measured on hardware**: 30535680 sectors, 14893 MB |
| Crash log | fatal errors survive the reset, `boswell fault` | **caught the bug it was written for** |
| Cursor save | moved off the writer thread onto `qspi-save` | **on hardware**: two backlogs drained while connected, 0 resets |
| Push button | gesture driver on D7, single press toggles capture | builds; 11 tests; **no switch to press yet** |
| Card capture | audio spills to FAT files past a 75% ring mark | **on hardware**: 600 KB written, header read back, 0 errors |
| Card capture | audio spills to FAT files past a 75% ring mark | **on hardware**: 600 KB written, header verified, 0 errors |
| OTA | `CTRL_DFU` sent from the app, gated on the capability bit | 8 tests; not yet triggered on hardware |
| Device selection | `BOSWELL_DEVICE` picks a board by address or name | 10 tests |
| De-duplication | `web/dedup.py`, `boot_id` in every times record | 18 tests; not wired to an ingest path |
| Tap controls | enable + threshold from the app | 6 tests |
| Card status | presence and free space in the UI | 7 tests |

## The watchdog resets — found, fixed, and why it took three tries

The Opus build reset a second or two after capture started, every time, and
reported `last reset=0x00000002 watchdog` with no explanation. It is a **stack
overflow in the `capture` thread**, and it is fixed.

**What it was.** Opus is compiled here with `USE_ALLOCA`, so `opus_encode()`
takes its working buffers from the stack of whoever calls it, and the amount
scales with the frame length. This project encodes 20 ms frames; Omi, on the
same part, encodes 10 ms and still gives its codec a dedicated 32,000-byte
thread. `capture` was handing it 4,096, then 16,384. Measured on the board
while streaming, one 20 ms CELT frame at 16 kHz needs **17,944 bytes**.

16,384 is 1,560 bytes short. That margin is why it was so hard to see: the
board booted, advertised, connected and idled perfectly, and only died once
audio started flowing through the encoder.

**Why it looked like a hang.** Three things conspired.

- `CONFIG_MPU_STACK_GUARD` turns the overrun into a fatal MPU fault, and
  Zephyr's default fatal handler **halts the CPU**.
- The console is **USB CDC**. USB stops being serviced the instant the CPU
  halts, so the panic banner is written into a buffer nothing will ever
  drain. `LOG_PANIC()` cannot help — there is no working transport left.
- With the CPU halted, nothing feeds the watchdog, so thirty seconds later
  the board resets and the reset register says `watchdog`.

From the outside: shell dies instantly, board reboots half a minute later,
no banner, reset reason `watchdog`. Which is exactly what a deadlock looks
like, and it was diagnosed as one twice.

**The measurement that misled.** `boswell stacks` reported `capture 80 of
16384 used, 99% headroom`, and that reading was taken as proof the stack was
innocent. It was taken **at the prompt, after the reboot**, where capture is
idle and the thread has touched 80 bytes. The same command run *while
streaming* reads 17,944 immediately. A stack high-water is only meaningful
under the load you are asking about.

**The fix, in two parts.**

1. `CAPTURE_STACK` is **24,576** on the Opus build — 6.6 KB over the measured
   high-water. Unchanged at 4,096 for ADPCM, which does not need it.
2. `src/fault.c` records the fatal error into `__noinit` RAM — which a soft
   or watchdog reset does not clear — and reboots instead of halting. The
   next boot logs one line saying what happened, and `boswell fault` repeats
   it on demand. This is what turns the next occurrence of this class of bug
   from an evening into a sentence:

   ```
   <err> main: last boot ended in a fault: stack overflow in thread
   'capture' at 13497 ms, pc=0x0006a878 lr=0xffffffff stack=16448
   ```

**Verified.** 11 minutes streaming, 0 reboots, high-water flat at 17,944 from
the first two seconds. See the soak below.

**The `persist_cursors()` exposure is now closed too.** It was a separate
fault from the stack overflow and was never the cause of it, but it was real:
an internal-flash write on the writer thread, scheduled around the radio by
MPSL, with no bound on how long it could wait. If it waited past the watchdog
window, `writer_fn` was blocked *inside* the save and `WDT_QSPI` never checked
in again.

The fix is the split that was identified as the right one and deferred:
deciding *what* to save stays on the writer, where the cursors live and the
work is arithmetic; the flash write moved to a `qspi-save` thread at
`K_PRIO_PREEMPT(13)` that no watchdog waits on. A snapshot is handed over
under a mutex held only for a struct copy -- the saver drops it before
touching flash, because a lock held across the slow part would put the writer
straight back where it started. A snapshot superseded before it reaches flash
is counted rather than lost, since newer cursors describe strictly more
drained backlog.

`boswell status` now reports `cursor saves=N coalesced=N worst=N ms`. The
middle number is the one to watch: it counts saves that fell behind the radio,
which is the condition that used to reset the board.

**Verified** under the exact condition that used to reset it -- armed and
disconnected until a backlog built, then a host connected at a short interval:

| backlog at connect | drained to | saves | coalesced | worst | resets |
|---|---|---|---|---|---|
| 608 KB | 2 B | 16 | 0 | 2 ms | **0** in 11.5 min |
| 219 KB | 2 B | 6 | 0 | 2 ms | **0** |

Before this, the same board went down roughly once a minute whenever it held
a backlog.

**Two stacks were raised on the way past, for the reason CAPTURE_STACK
taught.** Measured while connected and draining -- not at an idle prompt --
`qspi` was using 784 bytes of 1024 (23% headroom) and `qspi-save` 600 of 1024.
Neither was failing, and neither was a margin worth keeping on a device that
resets in the field with no console attached. They are 2048 and 1536 now, 61%
and 60% headroom at the same measured peaks, and `boswell stacks` reports both.

## The fork is decided: FAT32

Nathan took FAT, and the reason is the workflow he described before the
question was ever posed -- open a web page, see what is there, listen to it,
download it. Raw sectors are the better engineering, and they are what Omi
ships: even wear, nothing to corrupt when the battery goes. They also make
the card unmountable, which makes that workflow impossible. Omi chose raw
because they never intended anyone to plug the device into a computer.

The power-loss risk is real and mitigated rather than eliminated: writes are
batched 16 KB, `fs_sync()` commits the FAT and directory entry after every
batch, and every path that stops capture flushes. A battery pulled mid-batch
costs the last few seconds of one file, not the volume.

## Phase 02 is built and measured

Audio reaches the card. The design that matters is *when*:

- **Radio first.** Anything the host can take live goes over the air,
  because that becomes a conversation now rather than at the end of the day.
- **The ring is not bypassed.** The obvious version -- radio if connected,
  card if not -- would put audio on the card the moment you walked into the
  next room, turning a gap that used to heal itself on reconnect into one
  that waits for a cable. The 2 MB ring goes on covering short absences.
- **The card is the overflow.** Past 75% of the ring, the *oldest* records
  spill to files to make room. The ordering falls out correctly on its own:
  the ring drains oldest-first, so a day out of range ends with the morning
  on the card and the last few minutes still able to arrive live.

Files are `/SD:/boswell/b<bootid>_<nnnn>.bwl`, capped at 1 MB (about four
minutes of Opus), each opening with

    "BSWL" | ver:u8 | codec:u8 | rate:u16 | boot_id:u32 | reserved:u32

and then repeating `len:u16 | payload[len]`. The payload is a whole proto
frame, byte for byte as the radio would have carried it, so the host decodes
a docked file with the same code that decodes a live one. `boot_id` is in
the header because it is half the de-duplication key `web/dedup.py` already
uses -- `device_ms` restarts at zero every boot and cannot place a file on
its own.

**Measured on hardware**, with the spill mark temporarily dropped to 2% so it
could be reached in three minutes rather than after a day of talking:

| | |
|---|---|
| written | 600,335 bytes over ~200 s |
| write errors | 0 |
| worst single write | 203 ms -- the card stall the ring exists to absorb |
| ring while spilling | held flat at the mark, never grew |
| file read back | `ok, codec=20 rate=16000 boot=e9cd` |

`boswell card` reports what has been written; `boswell cardls` lists the
files and validates each header, because "the frame counter went up" is not
the same as "there is a readable recording on the card".

## Still ahead on the card

- **03 timestamps.** Files are named by boot id and sequence. A recording
  made at 3 a.m. on a walk still only knows it happened N minutes after
  boot; the host has to send wall-clock on connect and the device store the
  offset.
- **04 retention.** Nothing deletes anything yet. 14.9 GB is about 23 days
  at the Opus rate, and after that the card fills and writes start failing.
- **05 offload.** USB mass storage, now that the format is FAT. The files
  exist and there is no way to get them off except the shell listing.
- **06 ingest.** `web/dedup.py` has 18 tests and still nothing calls it,
  because there is no path that brings a file in.


## Waiting on hardware

**The push button is written and cannot be pressed.** `src/button.c` reads a
switch on D7 (P1.12) -- the one header pin free in every build here, since the
card takes D8/D9/D10 and D0, the speaker wants D1/D2/D3, and D4/D5 are the
I2C pair. Single press toggles capture; a switch is only ever pressed on
purpose, so the everyday action is the easy one rather than needing a pair the
way the accelerometer did. Double is detected and bound to nothing.

Long press is detected and **deliberately not wired to power-off**. The wake
path would be a GPIO sense on that same pin, and if it is wrong the device is
off until somebody finds the reset button -- which is a bad thing to ship
untested, and it cannot be tested until there is a switch. Once a real press
proves reliable it becomes `sys_poweroff()` with the pin as the wake source.

**Measured, and trimmed.** `boswell opus` on the board reports the encoder
wants **7,180 bytes** against the 20 KB that had been set aside — 2.9 times
larger than needed. `enc_mem` is now 8 KB, leaving about a kilobyte of
headroom and handing back twelve. RAM on the combined build went 60.63% to
55.94%.

The check at init is what makes tightening it safe: a build configured
differently — SILK, or stereo — would want far more, and would say so at
init rather than running off the end of the array.

**Opus is proven end to end.** Audio has been captured, encoded on the
board, transmitted, decoded by the host and transcribed correctly. The
encoder reserve is measured (7,180 bytes of the 8 KB set aside) and so is the
stack it encodes on (17,944 of 24,576).

## Building the variants

```bash
firmware/zephyr/build.sh                              # wearable, no card, ADPCM

EXTRA_OVERLAY=boards/audio_bff.overlay \
EXTRA_CONF="sdcard.conf;opus.conf" \
BUILD_DIR=/tmp/boswell-full-build \
  firmware/zephyr/build.sh                            # Audio BFF, card + Opus
```

The plain image size is the check that none of the card or codec work leaked
into the build the wearable runs. The baseline is **571,904 bytes** as of the card store.

It has moved five times, deliberately. From 571,392 for the saver thread and
its stack report, which any build with a backlog benefits from. Before that
from 569,856 for `src/button.c`: the switch is not fitted to the wearable
either, but the driver costs one configured input on a board without one and
the alternative is two firmwares that drift. Before that from 564,736 for
`src/fault.c` and
`boswell fault`: a wearable that resets in the field with no console attached
is precisely the case that record exists for, so it belongs in both builds.
Before that from 564,224 for `boswell stacks`, a thread high-water report that
any build benefits from. And before that, from 563,712: the backlog-clear command is not
card code or codec code, it is a general diagnostic that a wearable with a
stuck backlog needs just as much, so it belongs in both builds and costs 512
bytes there. Every other change to this number has meant something leaked and
should be treated that way.
