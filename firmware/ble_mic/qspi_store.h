/*
 * Store-and-forward buffer on the 2 MB onboard QSPI flash.
 *
 * When capture is armed but no host is connected, encoded frames go here
 * instead of being thrown away. On reconnect the backlog is drained to the
 * host first, then streaming resumes live. Walking out of Bluetooth range
 * costs you latency rather than the conversation.
 *
 * Layout is a circular byte stream of records:
 *
 *     [0xB5][len:u8][payload ...]
 *
 * The magic byte exists so the reader can resynchronise: when the writer laps
 * the reader, the oldest whole sector is dropped, which can leave the read
 * pointer mid-record. Scanning for 0xB5 and sanity-checking the length
 * recovers the stream rather than emitting garbage.
 *
 * Capacity at 8 kHz ADPCM (88-byte frames, 90 bytes on flash) is roughly
 * 23,000 frames -- about 7.7 minutes of continuous audio, or well over
 * twice that once VAD gating is on, since silence is never buffered.
 */

#ifndef QSPI_STORE_H
#define QSPI_STORE_H

#include <Adafruit_SPIFlash.h>
#include <SPI.h>

/* Record layout: [magic][len][crc8][payload] -- the same as the Zephyr store,
 * from the same rec_crc.h, so the two cannot drift. Magic and length alone
 * cannot tell a record from a coincidence: any byte has a one-in-256 chance
 * of being the magic value, and on the Zephyr side that is exactly how a
 * cursor pointing into the middle of a record was accepted as a valid one. */
#include "rec_crc.h"

#define QSPI_MAGIC        0xB5
#define QSPI_HDR_LEN      3
#define QSPI_PAGE         256
#define QSPI_SECTOR       4096
#define QSPI_MAX_PAYLOAD  200

/* Puya P25Q16H, 2 MiB. Every Seeed nRF52 variant names this part but the
 * bundled flash-device table does not define it, so declare it here. */
static const SPIFlash_Device_t P25Q16H_DEV = {
  .total_size = (1UL << 21),
  .start_up_time_us = 10000,
  .manufacturer_id = 0x85,
  .memory_type = 0x60,
  .capacity = 0x15,
  .max_clock_speed_mhz = 55,
  .quad_enable_bit_mask = 0x02,
  .has_sector_protection = false,
  .supports_fast_read = true,
  .supports_qspi = true,
  .supports_qspi_writes = true,
  .write_status_register_split = false,
  .single_status_byte = false,
  .is_fram = false,
};

static Adafruit_FlashTransport_QSPI qspiTransport;
static Adafruit_SPIFlashBase qspiFlash(&qspiTransport);

static bool     qspiReady    = false;
static uint32_t qspiCapacity = 0;
static uint32_t qspiWrite    = 0;      // byte offset of next write
static uint32_t qspiRead     = 0;      // byte offset of next read
static uint32_t qspiPending  = 0;      // bytes not yet drained
static uint32_t qspiDropped  = 0;      // records lost to wrap
static uint32_t qspiErased   = 0xFFFFFFFF;  // sector currently erased for writing

static uint8_t  qspiPage[QSPI_PAGE];
static uint16_t qspiPageLen = 0;
static uint32_t qspiPageAddr = 0;

/* Flash operations the part refused.
 *
 * Every erase, program and read result was discarded, so a flash that had
 * stopped accepting writes advanced the cursors exactly as though it had
 * not: the store reported audio queued that was never stored, and would
 * later hand back whatever the erased sectors happened to contain. Counting
 * is the difference between an empty backlog and a broken one. */
static uint32_t qspiWriteFails = 0;
static uint32_t qspiReadFails  = 0;
static uint32_t qspiCrcFails   = 0;

static uint8_t qspiPeekByte(uint32_t addr);
static bool    qspiReadWrapped(uint32_t addr, uint8_t *out, uint32_t len);

/* Where the backlog had got to, kept in the internal filesystem.
 *
 * The audio survives a reset -- it is in external flash -- but these cursors
 * did not, so a watchdog reset, a crash or a flat battery discarded the whole
 * backlog. Store-and-forward exists so that being out of range costs latency
 * rather than audio, and losing it to a reboot is the same loss by another
 * route. The Zephyr side keeps them in NVS for the same reason. */
#define QSPI_CURSOR_PATH  "/qspi_cursor.bin"
#define QSPI_CURSOR_MAGIC 0xB0C5
#define QSPI_CURSOR_VER   1
#define QSPI_CURSOR_SAVE_MS 60000

struct QspiCursor {
  uint16_t magic;
  uint8_t  version;
  uint8_t  pad;
  uint32_t write;
  uint32_t read;
  uint32_t pending;
};

static uint32_t qspiCursorSavedAt = 0;
static bool     qspiHadBacklog    = false;

static void qspiCursorSave() {
  QspiCursor c = { QSPI_CURSOR_MAGIC, QSPI_CURSOR_VER, 0,
                   qspiWrite, qspiRead, qspiPending };
  const char *tmp = QSPI_CURSOR_PATH ".new";
  InternalFS.remove(tmp);
  bool ok = false;
  {
    Adafruit_LittleFS_Namespace::File f(InternalFS);
    if (f.open(tmp, Adafruit_LittleFS_Namespace::FILE_O_WRITE)) {
      ok = f.write((const uint8_t *)&c, sizeof(c)) == (int)sizeof(c);
      f.close();
    }
  }
  if (!ok) { InternalFS.remove(tmp); return; }
  InternalFS.remove(QSPI_CURSOR_PATH);
  InternalFS.rename(tmp, QSPI_CURSOR_PATH);
}

/* Rate limited hard: this lands in internal flash and audio arrives at
   several KB/s while disconnected. Both edges matter -- the save when a
   backlog appears means a reset moments later still finds it, and the save
   when it empties means a reset after a good drain does not replay it. */
static void qspiCursorService() {
  bool has = qspiPending > 0;
  uint32_t now = millis();
  if (has != qspiHadBacklog) {
    qspiHadBacklog = has;
    qspiCursorSavedAt = now;
    qspiCursorSave();
    return;
  }
  if (has && now - qspiCursorSavedAt >= QSPI_CURSOR_SAVE_MS) {
    qspiCursorSavedAt = now;
    qspiCursorSave();
  }
}

static bool qspiBegin() {
  if (!qspiFlash.begin(&P25Q16H_DEV, 1)) {
    // Fall back to auto-detection in case a board ships a different part.
    if (!qspiFlash.begin()) return false;
  }
  qspiCapacity = qspiFlash.size();
  if (qspiCapacity < QSPI_SECTOR * 4) return false;
  qspiReady = true;
  qspiWrite = qspiRead = qspiPending = qspiDropped = 0;
  qspiPageLen = 0;
  qspiPageAddr = 0;
  qspiErased = 0xFFFFFFFF;

  /* Resume, but only if the flash agrees with what was written down. A whole
     record has to parse at the saved read cursor and its CRC has to check
     out; one matching magic byte is a one-in-256 coincidence, which is
     exactly how the Zephyr side wedged its backlog on a cursor that pointed
     into the middle of a record. Anything doubtful starts empty. */
  QspiCursor c;
  Adafruit_LittleFS_Namespace::File f(InternalFS);
  if (f.open(QSPI_CURSOR_PATH, Adafruit_LittleFS_Namespace::FILE_O_READ)) {
    bool got = f.read((uint8_t *)&c, sizeof(c)) == (int)sizeof(c);
    f.close();
    if (got && c.magic == QSPI_CURSOR_MAGIC && c.version == QSPI_CURSOR_VER &&
        c.pending > 0 && c.pending <= qspiCapacity) {
      uint8_t magic = qspiPeekByte(c.read);
      uint8_t len   = qspiPeekByte(c.read + 1);
      uint8_t crc   = qspiPeekByte(c.read + 2);
      if (magic == QSPI_MAGIC && len > 0 && len <= QSPI_MAX_PAYLOAD) {
        static uint8_t probe[QSPI_MAX_PAYLOAD];
        if (qspiReadWrapped(c.read + QSPI_HDR_LEN, probe, len) &&
            rec_crc8(probe, len) == crc) {
          qspiWrite   = c.write;
          qspiRead    = c.read;
          qspiPending = c.pending;
          qspiHadBacklog = true;
        }
      }
    }
  }
  return true;
}

/* Erase the sector containing `addr` if we have not already erased it for
 * this pass. NOR flash only clears bits on erase, so a sector must be wiped
 * before it can be rewritten. */
static bool qspiEnsureErased(uint32_t addr) {
  uint32_t sector = addr / QSPI_SECTOR;
  if (sector == qspiErased) return true;
  if (!qspiFlash.eraseSector(sector)) {
    qspiWriteFails++;
    return false;
  }
  qspiErased = sector;
  return true;
}

static void qspiCommitPage() {
  if (qspiPageLen == 0) return;
  /* Advance only on success.
   *
   * The cursor moved whether or not the page landed, so a refused erase or
   * program left a hole the reader would later walk into and read as audio.
   * Keeping the page means the next attempt rewrites it; the staging side
   * stops taking new bytes because the page is still full. */
  if (!qspiEnsureErased(qspiPageAddr)) {
    return;
  }
  if (!qspiFlash.writeBuffer(qspiPageAddr, qspiPage, qspiPageLen)) {
    qspiWriteFails++;
    return;
  }
  qspiPageAddr = (qspiPageAddr + QSPI_PAGE) % qspiCapacity;
  qspiPageLen = 0;
}

static void qspiPutByte(uint8_t b) {
  if (qspiPageLen == 0) qspiPageAddr = qspiWrite;
  qspiPage[qspiPageLen++] = b;
  qspiWrite = (qspiWrite + 1) % qspiCapacity;
  qspiPending++;
  if (qspiPageLen == QSPI_PAGE) qspiCommitPage();
}

/* Append one frame. Returns false only if the store is unusable. */
static bool qspiPush(const uint8_t *data, uint8_t len) {
  if (!qspiReady || len == 0 || len > QSPI_MAX_PAYLOAD) return false;

  // Writer about to lap the reader: drop the oldest sector rather than
  // corrupting the stream. Recent audio matters more than old audio.
  if (qspiPending + len + 2 > qspiCapacity - QSPI_SECTOR) {
    qspiRead = (qspiRead + QSPI_SECTOR) % qspiCapacity;
    qspiPending = (qspiPending > QSPI_SECTOR) ? qspiPending - QSPI_SECTOR : 0;
    qspiDropped++;
  }

  qspiPutByte(QSPI_MAGIC);
  qspiPutByte(len);
  qspiPutByte(rec_crc8(data, len));
  for (uint8_t i = 0; i < len; i++) qspiPutByte(data[i]);
  return true;
}

/* Read len bytes from a logical address, splitting the read where the flash
 * ends.
 *
 * The header above is read a byte at a time through modular addressing and so
 * wraps correctly, but the payload was one readBuffer() at (addr % capacity)
 * for the full length -- which runs off the end of the device for any record
 * that straddles address zero. That is one record out of every lap of the
 * ring, and only ever at the moment the buffer is fullest. The same fault in
 * the Zephyr store was the other way round: payload wrapped, header did not.
 */
static bool qspiReadWrapped(uint32_t addr, uint8_t *out, uint32_t len) {
  addr %= qspiCapacity;
  uint32_t first = qspiCapacity - addr;
  bool ok;
  if (first >= len) {
    ok = qspiFlash.readBuffer(addr, out, len) == len;
  } else {
    ok = qspiFlash.readBuffer(addr, out, first) == first &&
         qspiFlash.readBuffer(0, out + first, len - first) == (len - first);
  }
  if (!ok) {
    qspiReadFails++;
  }
  return ok;
}

static uint8_t qspiPeekByte(uint32_t addr) {
  uint8_t b = 0;
  qspiFlash.readBuffer(addr % qspiCapacity, &b, 1);
  return b;
}

/* Pop the oldest frame. Returns 0 when empty or unrecoverable. */
static uint8_t qspiPop(uint8_t *out, uint8_t maxLen) {
  if (!qspiReady || qspiPending == 0) return 0;
  qspiCommitPage();            // make in-RAM bytes visible to the reader

  // Resynchronise on the magic byte; a dropped sector can leave us mid-record.
  uint32_t scanned = 0;
  while (qspiPending >= QSPI_HDR_LEN && scanned < QSPI_SECTOR) {
    if (qspiPeekByte(qspiRead) == QSPI_MAGIC) {
      uint8_t len = qspiPeekByte(qspiRead + 1);
      uint8_t crc = qspiPeekByte(qspiRead + 2);
      if (len > 0 && len <= QSPI_MAX_PAYLOAD && len <= maxLen &&
          qspiPending >= (uint32_t)len + QSPI_HDR_LEN) {
        if (!qspiReadWrapped(qspiRead + QSPI_HDR_LEN, out, len)) {
          return 0;              /* left in place; the next pass retries */
        }
        if (rec_crc8(out, len) == crc) {
          qspiRead = (qspiRead + QSPI_HDR_LEN + len) % qspiCapacity;
          qspiPending -= (len + QSPI_HDR_LEN);
          return len;
        }
        qspiCrcFails++;          /* a coincidence, not a record: scan on */
      }
    }
    qspiRead = (qspiRead + 1) % qspiCapacity;
    qspiPending--;
    scanned++;
  }
  return 0;
}

static void qspiClear() {
  qspiWrite = qspiRead = qspiPending = 0;
  qspiPageLen = 0;
  qspiErased = 0xFFFFFFFF;
}

static bool     qspiIsReady()    { return qspiReady; }
static uint32_t qspiPendingBytes(){ return qspiPending; }
static uint32_t qspiSizeBytes()  { return qspiCapacity; }
static uint32_t qspiDropCount()  { return qspiDropped; }

#endif
