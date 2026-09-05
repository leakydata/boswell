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

**What this does not change.** The `persist_cursors()` exposure is real and
still there — an internal-flash write on the writer thread can block past the
watchdog window when the radio is busy. It is now instrumented (a save over
500 ms warns) but not moved off that thread. It was *not* the cause of this
fault: the earlier "backlog cleared, 31 minutes clean" runs were idle and
disconnected, so the encoder never ran and the test could not have failed.

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
into the build the wearable runs. The baseline is **566,784 bytes** as of the crash log.

It has moved three times, deliberately. From 564,736 for `src/fault.c` and
`boswell fault`: a wearable that resets in the field with no console attached
is precisely the case that record exists for, so it belongs in both builds.
Before that from 564,224 for `boswell stacks`, a thread high-water report that
any build benefits from. And before that, from 563,712: the backlog-clear command is not
card code or codec code, it is a general diagnostic that a wearable with a
stuck backlog needs just as much, so it belongs in both builds and costs 512
bytes there. Every other change to this number has meant something leaked and
should be treated that way.
