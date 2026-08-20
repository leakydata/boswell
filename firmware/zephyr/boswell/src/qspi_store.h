/*
 * Store-and-forward buffer on the 2 MB onboard QSPI flash.
 *
 * When capture is armed but no host is connected, encoded frames go here
 * instead of being thrown away. On reconnect the backlog drains to the host
 * first, then streaming resumes live. Walking out of Bluetooth range costs
 * latency rather than the conversation.
 *
 * Layout is a circular byte stream of records:
 *
 *     [0xB5][len:u8][payload ...]
 *
 * The magic byte exists so the reader can resynchronise. When the writer laps
 * the reader the oldest whole sector is dropped, which can leave the read
 * pointer mid-record; scanning for 0xB5 and sanity-checking the length
 * recovers the stream rather than emitting garbage.
 *
 * Capacity at 8 kHz ADPCM (88-byte frames, 90 bytes on flash) is roughly
 * 23,000 frames -- about 7.7 minutes of continuous audio, or well over twice
 * that with VAD gating on, since silence is never buffered.
 *
 * Writing happens while disconnected and draining while connected, so the two
 * do not interleave; the partial page is flushed once when a drain starts.
 */

#ifndef BOSWELL_QSPI_STORE_H
#define BOSWELL_QSPI_STORE_H

#include <stdbool.h>
#include <stdint.h>

/* Record layout: [magic][len][crc8][payload]
 *
 * The CRC exists because magic-and-length is not enough to tell a record from
 * a coincidence. Any byte has a one-in-256 chance of being the magic value,
 * and the byte after it a good chance of being a plausible length -- which is
 * exactly how a read cursor pointing into the middle of a record was accepted
 * as a valid one, produced a record too short to deliver, and wedged the
 * backlog until somebody went looking. A checksum over the payload turns that
 * coincidence into a one-in-65536 one, and a mismatch into a resync rather
 * than a stall.
 */
#define QSPI_MAGIC       0xB5
#define QSPI_MAX_PAYLOAD 200
#define QSPI_HDR_LEN     3

int      qspi_store_init(void);
bool     qspi_store_ready(void);
uint32_t qspi_store_pending(void);
/* read failures, short records, oversize, bytes scanned, last errno */
void qspi_store_pop_stats(uint32_t out[9], int *last_err);
/* write failures, erase failures, last errno */
void qspi_store_write_stats(uint32_t out[2], int *last_err);     /* bytes not yet drained */
uint32_t qspi_store_capacity(void);
uint32_t qspi_store_dropped(void);     /* records lost to lapping */

/* Called from the writer thread to hand a replayed record to the link.
 * Returns false to stop draining for now (link busy or gone). Draining lives
 * here rather than in the capture thread because popping takes the same lock
 * the writer holds across a sector erase, and blocking capture on that
 * overruns the microphone. */
/* Hand one buffered record to the host.
 *
 *   > 0  delivered; the store may drop it
 *   = 0  not now -- the radio is busy; offer the same record again
 *   < 0  this record cannot ever be delivered; skip it
 *
 * The middle and last cases used to be the same value, so a record the host
 * could never accept was retried forever and the backlog stopped moving.
 */
typedef int (*qspi_drain_fn)(const uint8_t *rec, uint16_t len);
/* Asked before a record is popped. Popping advances the read pointer, so a
 * record taken out while the link cannot accept it is simply lost -- the
 * first version discarded about fifty records a second that way, exactly
 * cancelling out what capture was storing. */
typedef bool (*qspi_ready_fn)(void);
void     qspi_store_set_drain(qspi_drain_fn fn, qspi_ready_fn ready);

int      qspi_store_push(const uint8_t *data, uint8_t len);
/* Peek reads the next record without consuming it; commit discards it once
 * the host has actually taken it. Popping first and delivering afterwards
 * turned a moment of radio backpressure into permanently lost recovered
 * audio, because the record was already gone when the send failed. */
int      qspi_store_peek(uint8_t *out, uint8_t max_len);
void     qspi_store_commit(uint8_t len);

/* Returns payload length, or 0 when empty. Flushes any partial page. */
int      qspi_store_pop(uint8_t *out, uint8_t max_len);
/* Empties the store. Runs on the writer thread, because the staging ring has
 * one consumer and clearing it from anywhere else would break that. */
void     qspi_store_reset(void);
/* pushes, pages written, sector erases, writer wakeups */
void     qspi_store_stats(uint32_t out[4]);
/* Called from the writer thread each time round its loop, so the watchdog
 * has evidence it is still running rather than an assumption. */
void     qspi_store_set_alive_cb(void (*cb)(void));

#endif
