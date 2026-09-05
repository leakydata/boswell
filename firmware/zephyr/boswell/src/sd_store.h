#ifndef BOSWELL_SD_STORE_H
#define BOSWELL_SD_STORE_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Audio onto the card, as files.
 *
 * The 2 MB QSPI ring holds 4.4 minutes. That is enough to walk out of
 * Bluetooth range and come back; it is not enough to spend a day away from
 * the computer, which is the point of fitting a card. The ring stays exactly
 * where it is, in front of this -- cards stall for 100 ms or more on their
 * own housekeeping, and a buffer that deep is the difference between a
 * hiccup and a hole in the audio.
 *
 * FAT, not raw sectors. Raw is the better engineering -- even wear, nothing
 * to corrupt when the battery goes -- and it is what Omi ships, but it makes
 * the card unmountable, and being able to dock the device and read the files
 * is the whole reason for the card being here.
 *
 * File layout:
 *
 *     "BSWL" | ver:u8 | codec:u8 | rate:u16 | boot_id:u32 | reserved:u32
 *     then repeating:  len:u16 | payload[len]
 *
 * The payload is a whole proto frame, byte for byte as the radio would have
 * carried it, so the host decodes a docked file with the same code that
 * decodes a live one.
 */

#define SD_FILE_MAGIC   "BSWL"
#define SD_FILE_VERSION 1
#define SD_HEADER_LEN   16

struct sd_store_stats {
    uint32_t frames;        /* frames written */
    uint32_t bytes;         /* payload bytes written */
    uint32_t files;         /* files opened */
    uint32_t write_errs;
    uint32_t worst_ms;      /* longest single flush */
    int      last_err;
};

#ifdef CONFIG_DISK_DRIVER_SDMMC

/* Prepare to record. Safe before the card has mounted -- writes are refused
 * until it has. */
void sd_store_init(void);

/* True when a write has somewhere to go. */
bool sd_store_ready(void);

/* Append one proto frame.
 *
 * Same three-way answer as the QSPI drain, and for the same reason:
 *   > 0  written (or buffered for writing)
 *   = 0  not now -- offer it again
 *   < 0  never writable; skip it
 */
int sd_store_write(const uint8_t *rec, uint16_t len);

/* Push the batch out and close the current file. Called when capture stops,
 * so a device that is put down has its audio on the card rather than in RAM. */
void sd_store_flush(void);

/* Tell the store what it is recording, so the file header is right. A change
 * closes the current file, because one file describes one format. */
void sd_store_format(uint8_t codec, uint16_t rate, uint32_t boot_id);

void sd_store_get_stats(struct sd_store_stats *out);

/* List what is on the card, validating each file's header as it goes.
 *
 * "The frame counter went up" is not the same as "there is a readable
 * recording on the card" -- the whole point of choosing FAT was that these
 * files get read by something else later, and a header written wrongly would
 * not show up until the day somebody tried. This opens each one and checks
 * the magic, version, codec and rate actually parse.
 */
struct shell;
int sd_store_list(const struct shell *sh);

#else  /* no card slot on this board */

/* A build for a board with no card slot still has a drain path that asks
 * where a record should go. Stubs here rather than #ifdefs at every call
 * site: "there is no card" is a perfectly good answer to all of these, and
 * the wearable build wants the same drain logic without knowing why.
 *
 * sd_store_write() returns 0 -- "not now" -- rather than -1. A record that
 * can never be written is one the ring should drop, and on this build every
 * record is in that position, so -1 would quietly discard the backlog that
 * exists precisely to survive until the radio comes back.
 */
static inline void sd_store_init(void) { }
static inline bool sd_store_ready(void) { return false; }
static inline int  sd_store_write(const uint8_t *rec, uint16_t len)
{
    (void)rec; (void)len; return 0;
}
static inline void sd_store_flush(void) { }
static inline void sd_store_format(uint8_t c, uint16_t r, uint32_t b)
{
    (void)c; (void)r; (void)b;
}
static inline void sd_store_get_stats(struct sd_store_stats *out)
{
    if (out) { *out = (struct sd_store_stats){ 0 }; }
}
struct shell;
static inline int sd_store_list(const struct shell *sh) { (void)sh; return -1; }

#endif /* CONFIG_DISK_DRIVER_SDMMC */

#endif /* BOSWELL_SD_STORE_H */
