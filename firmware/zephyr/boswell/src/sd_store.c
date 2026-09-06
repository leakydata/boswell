#include "sd_store.h"
#include "sd_probe.h"
#include "proto.h"
#include "rec_crc.h"
#include "clock.h"
#include "ble_audio.h"

#include <zephyr/kernel.h>
#include <zephyr/fs/fs.h>
#include <zephyr/logging/log.h>
#include <zephyr/shell/shell.h>

#include <stdio.h>
#include <string.h>

LOG_MODULE_REGISTER(boswell_sdstore, LOG_LEVEL_INF);

#define DIR      SD_MOUNT_POINT "/boswell"

/* One flush is 16 KB, which is Omi's figure and a sensible one: it is 32
 * sectors, so the card writes whole erase-block-friendly runs instead of
 * read-modify-writing a sector per frame, and at the Opus rate it is about
 * four seconds of audio per flush. Larger would mean more audio in RAM at
 * the moment the battery goes. */
#define BATCH    (16 * 1024)

/* About four minutes of Opus per file. Small enough that losing the tail of
 * one to a flat battery is a nuisance rather than a loss, and large enough
 * that a day is a few hundred files rather than thousands. */
#define FILE_MAX (1024 * 1024)

/* Long enough that an ordinary card stall is ridden out, short enough that
 * the writer thread keeps feeding its watchdog if the card has gone away.
 * A refusal is not a loss: the record stays in the QSPI ring. */
#define LOCK_MS  200

/* Delete the oldest recordings when free space falls below this.
 *
 * Not delete-on-upload. 14.9 GB is about 23 days at the Opus rate, so there
 * is no space pressure worth reacting to, and prompt deletion buys nothing
 * while costing the only copy: an upload that turns out to be truncated or
 * mis-ingested can be re-read from the card for as long as the card still
 * holds it. Ageing out at the far end gives that for free.
 *
 * 1 GB is still a day and a half of headroom, which is enough that the
 * warning in the app is seen long before anything is actually removed. */
#define FREE_FLOOR_MB 1024

/* Checking free space walks the whole allocation table, so it is not
 * something to do per batch. Every few minutes is far more often than 1 GB
 * can be consumed at 4 kB/s. */
#define REAP_EVERY_MS (5 * 60 * 1000)

static uint8_t  batch[BATCH];
static size_t   batch_len;

static struct fs_file_t file;
static bool     file_open;
static size_t   file_len;

static uint8_t  fmt_codec = PROTO_CODEC_ADPCM;
static uint16_t fmt_rate  = 16000;
static uint32_t fmt_boot;
static bool     fmt_changed;

static uint32_t n_frames, n_bytes, n_files, n_write_errs, worst_ms;
static uint32_t n_reaped;
static int64_t  last_reap_ms;
static int      last_err;
static bool     started;

static void put16(uint8_t *p, uint16_t v) { p[0] = v & 0xff; p[1] = v >> 8; }
static void put32(uint8_t *p, uint32_t v)
{
    p[0] = v & 0xff; p[1] = (v >> 8) & 0xff;
    p[2] = (v >> 16) & 0xff; p[3] = (v >> 24) & 0xff;
}

/* Caller holds sd_lock. */
static uint32_t file_seq;
static void reap_if_low(void);

static int open_next(void)
{

    /* mkdir every time rather than once: the card can be pulled and a
     * different one put in, and -EEXIST costs nothing. */
    fs_mkdir(DIR);

    char path[64];
    snprintf(path, sizeof(path), DIR "/b%08x_%04u.bwl",
             (unsigned)fmt_boot, (unsigned)(file_seq++));

    fs_file_t_init(&file);
    int err = fs_open(&file, path, FS_O_CREATE | FS_O_WRITE);
    if (err) {
        last_err = err;
        n_write_errs++;
        LOG_ERR("open %s: %d", path, err);
        return err;
    }

    uint8_t hdr[SD_HEADER_LEN] = { 0 };
    memcpy(hdr, SD_FILE_MAGIC, 4);
    hdr[4] = SD_FILE_VERSION;
    hdr[5] = fmt_codec;
    put16(&hdr[6], fmt_rate);
    put32(&hdr[8], fmt_boot);
    /* Read when the file opens, not when it is written: if a host connects
     * mid-file the offset appears from nowhere, and a file whose header says
     * one thing about its first half and another about its second is worse
     * than one that honestly says it never knew. Zero means unknown, and the
     * host places those by sequence instead of by a time nobody set. */
    put32(&hdr[12], clock_boot_epoch());

    /* Zeroes if Bluetooth has not started. The first file of a boot can open
     * before it has, and a file that admits it does not know which device
     * made it is worth more than one that invents an answer. */
    (void)ble_audio_device_id(&hdr[16]);

    ssize_t w = fs_write(&file, hdr, sizeof(hdr));
    if (w != (ssize_t)sizeof(hdr)) {
        last_err = (int)w;
        n_write_errs++;
        fs_close(&file);
        return -EIO;
    }

    file_open = true;
    file_len  = sizeof(hdr);
    n_files++;
    LOG_INF("recording to %s", path);
    return 0;
}

/* Caller holds sd_lock. */
static int flush_batch(void)
{
    if (batch_len == 0) {
        return 0;
    }
    if (!file_open) {
        int err = open_next();
        if (err) {
            return err;
        }
    }

    int64_t t0 = k_uptime_get();
    ssize_t w = fs_write(&file, batch, batch_len);
    int64_t took = k_uptime_get() - t0;

    if (took > (int64_t)worst_ms) {
        worst_ms = (uint32_t)took;
    }
    if (took > 1000) {
        /* Not dangerous -- the QSPI ring is buffering behind this, which is
         * the reason it stayed there -- but a card taking this long is worth
         * knowing about before it starts dropping audio. */
        LOG_WRN("card write took %lld ms", took);
    }

    if (w != (ssize_t)batch_len) {
        last_err = (int)w;
        n_write_errs++;
        LOG_ERR("write %u bytes: %d", (unsigned)batch_len, (int)w);
        /* Close it. A file that took a short write has a torn record at the
         * end, and continuing to append would bury that inside a file the
         * host will try to read straight through. */
        fs_close(&file);
        file_open = false;
        batch_len = 0;
        return -EIO;
    }

    file_len += batch_len;
    batch_len = 0;

    /* fs_sync commits the FAT and directory entry. Without it a file that
     * was written but never closed has zero length after a power loss --
     * the data is in clusters nothing points at. */
    fs_sync(&file);

    if (file_len >= FILE_MAX) {
        fs_close(&file);
        file_open = false;
    }

    /* After the write, not before: making room is only worth doing once the
     * audio in hand is safely down. */
    reap_if_low();
    return 0;
}

/* Delete the oldest recording. Caller holds sd_lock.
 *
 * Oldest by name, which is the same as oldest in time: the sequence number
 * in b<boot>_<nnnn>.bwl only ever increases within a run, and a new run gets
 * a new boot id. It is not a perfect ordering across boots -- boot ids are
 * random -- but it does not need to be. The question being answered is
 * "remove something to make room", not "remove exactly the oldest thing",
 * and every candidate is at least as old as the file being written.
 */
static bool reap_one(void)
{
    struct fs_dir_t dir;
    fs_dir_t_init(&dir);
    if (fs_opendir(&dir, DIR) != 0) {
        return false;
    }

    char oldest[MAX_FILE_NAME + 1] = { 0 };

    for (;;) {
        struct fs_dirent ent;
        if (fs_readdir(&dir, &ent) != 0 || ent.name[0] == '\0') {
            break;
        }
        if (ent.type != FS_DIR_ENTRY_FILE) {
            continue;
        }
        if (oldest[0] == '\0' || strcmp(ent.name, oldest) < 0) {
            strncpy(oldest, ent.name, sizeof(oldest) - 1);
        }
    }
    fs_closedir(&dir);

    if (oldest[0] == '\0') {
        return false;
    }

    char path[sizeof(DIR) + 1 + MAX_FILE_NAME + 1];
    snprintf(path, sizeof(path), DIR "/%s", oldest);

    /* Never the file being written into. Deleting the open one would leave
     * the handle pointing at clusters the allocator has handed back. */
    if (file_open) {
        char cur[sizeof(DIR) + 1 + MAX_FILE_NAME + 1];
        snprintf(cur, sizeof(cur), DIR "/b%08x_%04u.bwl",
                 (unsigned)fmt_boot, (unsigned)(file_seq - 1));
        if (strcmp(path, cur) == 0) {
            return false;
        }
    }

    int err = fs_unlink(path);
    if (err) {
        LOG_WRN("could not remove %s: %d", path, err);
        return false;
    }
    n_reaped++;
    LOG_INF("removed %s to make room", oldest);
    return true;
}

/* Caller holds sd_lock. */
static void reap_if_low(void)
{
    int64_t now = k_uptime_get();
    if (last_reap_ms != 0 && now - last_reap_ms < REAP_EVERY_MS) {
        return;
    }
    last_reap_ms = now;

    struct fs_statvfs st;
    if (fs_statvfs(SD_MOUNT_POINT, &st) != 0) {
        return;
    }

    uint64_t free_mb = ((uint64_t)st.f_bfree * st.f_frsize) / (1024 * 1024);
    if (free_mb >= FREE_FLOOR_MB) {
        return;
    }

    /* A handful per pass, not until it is satisfied. Each unlink walks the
     * allocation table, and this runs on the thread that writes audio; the
     * next pass is five minutes away and 1 GB is a day and a half. */
    LOG_WRN("card down to %llu MB, removing oldest recordings", free_mb);
    for (int i = 0; i < 4 && reap_one(); i++) {
    }
}

void sd_store_init(void)
{
    batch_len = 0;
    started = true;
}

bool sd_store_ready(void)
{
    /* Released means a USB host owns the volume. Writing then is not "risky"
     * -- FATFS is unmounted, so there is nothing to write through, and the
     * caches that would be committed describe a card the host has been
     * editing underneath us. */
    return started && sd_mounted() && !sd_is_released();
}

void sd_store_format(uint8_t codec, uint16_t rate, uint32_t boot_id)
{
    if (codec == fmt_codec && rate == fmt_rate && boot_id == fmt_boot) {
        return;
    }
    fmt_codec = codec;
    fmt_rate  = rate;
    fmt_boot  = boot_id;
    /* One file describes one format, so the current one has to end here.
     * Done on the next write, which is the next time the lock is held. */
    fmt_changed = true;
}

int sd_store_write(const uint8_t *rec, uint16_t len)
{
    if (!sd_store_ready()) {
        return 0;                 /* not yet -- keep it in the ring */
    }
    if (len == 0 || len > MAX_FRAME_LEN) {
        return -1;                /* never writable */
    }

    if (k_mutex_lock(&sd_lock, K_MSEC(LOCK_MS)) != 0) {
        /* The card is busy mounting or counting clusters. The record is
         * still in the ring; it will be offered again. */
        return 0;
    }

    int rc = 1;

    if (fmt_changed) {
        fmt_changed = false;
        if (batch_len) {
            flush_batch();
        }
        if (file_open) {
            fs_close(&file);
            file_open = false;
        }
    }

    if (batch_len + 3 + len > sizeof(batch)) {
        if (flush_batch() != 0) {
            /* Say "not now" rather than "never": a card that failed one
             * write may take the next. The ring keeps the record, and if the
             * card stays broken the ring laps and drops it, which is the
             * same thing that happened before there was a card at all. */
            k_mutex_unlock(&sd_lock);
            return 0;
        }
    }

    put16(&batch[batch_len], len);
    batch_len += 2;

    uint8_t *payload = &batch[batch_len + 1];
    memcpy(payload, rec, len);

    /* The file header says which boot this file belongs to; the flag says it
     * per frame. Both, because a file can be read on its own and a frame can
     * be lifted out of one. Stamped before the checksum, or the checksum
     * would be over bytes that are not the ones written. */
    if (fmt_boot == 0 && len > 2) {
        payload[2] |= FLAG_PRE_BOOT;
    }

    batch[batch_len] = rec_crc8(payload, (uint8_t)len);
    batch_len += 1 + len;

    n_frames++;
    n_bytes += len;

    k_mutex_unlock(&sd_lock);
    return rc;
}

void sd_store_flush(void)
{
    if (!started) {
        return;
    }
    /* Longer than the write path waits. This is called when capture stops,
     * not per frame, and the point of it is that audio in RAM reaches the
     * card before the device is put down. */
    if (k_mutex_lock(&sd_lock, K_MSEC(5000)) != 0) {
        LOG_WRN("flush: card busy, %u bytes still in RAM", (unsigned)batch_len);
        return;
    }
    flush_batch();
    if (file_open) {
        fs_close(&file);
        file_open = false;
    }
    k_mutex_unlock(&sd_lock);
}

void sd_store_get_stats(struct sd_store_stats *out)
{
    if (!out) {
        return;
    }
    out->frames     = n_frames;
    out->bytes      = n_bytes;
    out->files      = n_files;
    out->write_errs = n_write_errs;
    out->worst_ms   = worst_ms;
    out->reaped     = n_reaped;
    out->last_err   = last_err;
}

int sd_store_list(const struct shell *sh)
{
    if (!sd_mounted()) {
        shell_print(sh, "card: not mounted");
        return -1;
    }

    /* Longer than the write path waits, because this is a person asking and
     * they would rather wait than be told the card was busy. */
    if (k_mutex_lock(&sd_lock, K_MSEC(5000)) != 0) {
        shell_print(sh, "card: busy");
        return -1;
    }

    /* Anything still in RAM is part of the answer to "what is on the card",
     * and leaving it out makes the newest file look short. */
    flush_batch();

    struct fs_dir_t dir;
    fs_dir_t_init(&dir);

    int err = fs_opendir(&dir, DIR);
    if (err) {
        shell_print(sh, "card: no recordings yet (%d)", err);
        k_mutex_unlock(&sd_lock);
        return err;
    }

    unsigned n = 0, bad = 0;
    uint64_t total = 0;

    for (;;) {
        struct fs_dirent ent;
        if (fs_readdir(&dir, &ent) != 0 || ent.name[0] == '\0') {
            break;
        }
        if (ent.type != FS_DIR_ENTRY_FILE) {
            continue;
        }

        /* Sized for the worst case FAT can hand back rather than for the
         * names this writes: a card that has been in something else may
         * carry long filenames, and a truncated path would open the wrong
         * file or none at all. */
        char path[sizeof(DIR) + 1 + MAX_FILE_NAME + 1];
        snprintf(path, sizeof(path), DIR "/%s", ent.name);

        struct fs_file_t f;
        fs_file_t_init(&f);

        uint8_t hdr[SD_HEADER_LEN];
        const char *verdict = "unreadable";
        unsigned codec = 0, rate = 0, boot = 0;

        if (fs_open(&f, path, FS_O_READ) == 0) {
            if (fs_read(&f, hdr, sizeof(hdr)) == (ssize_t)sizeof(hdr)) {
                if (memcmp(hdr, SD_FILE_MAGIC, 4) != 0) {
                    verdict = "bad magic";
                } else if (hdr[4] == 0 || hdr[4] > SD_FILE_VERSION) {
                    /* Newer than this firmware understands. Older is fine
                     * and is not "unreadable": the host reader accepts every
                     * version this project has written, and calling a file
                     * we wrote ourselves last week corrupt is how a real
                     * recording gets deleted by somebody tidying up. */
                    verdict = "newer than this firmware";
                } else {
                    codec = hdr[5];
                    rate  = hdr[6] | (hdr[7] << 8);
                    boot  = hdr[8] | (hdr[9] << 8) |
                            (hdr[10] << 16) | ((uint32_t)hdr[11] << 24);
                    verdict = "ok";
                }
            } else {
                verdict = "short";
            }
            fs_close(&f);
        }

        if (strcmp(verdict, "ok") != 0) {
            bad++;
        }
        n++;
        total += ent.size;

        shell_print(sh, "  %-24s %8u B  v%u %s%s codec=%u rate=%u boot=%04x",
                    ent.name, (unsigned)ent.size, hdr[4], verdict,
                    strcmp(verdict, "ok") ? "" : ",", codec, rate, boot);
    }

    fs_closedir(&dir);
    k_mutex_unlock(&sd_lock);

    shell_print(sh, "card: %u file(s), %llu bytes, %u unreadable", n, total, bad);
    return bad == 0 ? 0 : -1;
}
