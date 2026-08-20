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

#define QSPI_MAGIC        0xB5
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
  return true;
}

/* Erase the sector containing `addr` if we have not already erased it for
 * this pass. NOR flash only clears bits on erase, so a sector must be wiped
 * before it can be rewritten. */
static void qspiEnsureErased(uint32_t addr) {
  uint32_t sector = addr / QSPI_SECTOR;
  if (sector == qspiErased) return;
  qspiFlash.eraseSector(sector);
  qspiErased = sector;
}

static void qspiCommitPage() {
  if (qspiPageLen == 0) return;
  qspiEnsureErased(qspiPageAddr);
  qspiFlash.writeBuffer(qspiPageAddr, qspiPage, qspiPageLen);
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
static void qspiReadWrapped(uint32_t addr, uint8_t *out, uint32_t len) {
  addr %= qspiCapacity;
  uint32_t first = qspiCapacity - addr;
  if (first >= len) {
    qspiFlash.readBuffer(addr, out, len);
  } else {
    qspiFlash.readBuffer(addr, out, first);
    qspiFlash.readBuffer(0, out + first, len - first);
  }
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
  while (qspiPending >= 2 && scanned < QSPI_SECTOR) {
    if (qspiPeekByte(qspiRead) == QSPI_MAGIC) {
      uint8_t len = qspiPeekByte(qspiRead + 1);
      if (len > 0 && len <= QSPI_MAX_PAYLOAD && len <= maxLen &&
          qspiPending >= (uint32_t)len + 2) {
        qspiReadWrapped(qspiRead + 2, out, len);
        qspiRead = (qspiRead + 2 + len) % qspiCapacity;
        qspiPending -= (len + 2);
        return len;
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
