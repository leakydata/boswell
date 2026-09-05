#include "sd_xfer.h"
#include "sd_probe.h"
#include "sd_store.h"
#include "ble_audio.h"
#include "proto.h"

#include <zephyr/kernel.h>
#include <zephyr/fs/fs.h>
#include <zephyr/logging/log.h>

#include <stdio.h>
#include <string.h>

LOG_MODULE_REGISTER(boswell_xfer, LOG_LEVEL_INF);

#if defined(CONFIG_DISK_DRIVER_SDMMC)

#define DIR SD_MOUNT_POINT "/boswell"

/* One notification of payload. The negotiated MTU is larger than this on
 * every host that has connected, and a chunk that does not fit is split by
 * the stack into two -- which costs a connection event and halves the rate.
 * Deliberately conservative rather than negotiated: getting it wrong is a
 * slow transfer, and the transfer is already the slow way to do this. */
#define CHUNK 180

#define XFER_STACK 2048

static K_THREAD_STACK_DEFINE(xfer_stack, XFER_STACK);
static struct k_thread xfer_thread;

/* One command at a time. A second arriving while the first runs replaces it,
 * which is what a person impatiently pressing a button means by it. */
static K_SEM_DEFINE(work, 0, 1);
static atomic_t pending_cmd;
static atomic_t pending_arg;
static atomic_t abort_flag;
static atomic_t busy;

static uint32_t n_lists, n_reads, n_bytes, n_aborts;

static uint8_t buf[4 + CHUNK];

static void reply(const uint8_t *data, uint16_t len)
{
    (void)ble_audio_send_files(data, len);
}

static void reply_error(int err)
{
    uint8_t e[2] = { FILES_ERROR, (uint8_t)(int8_t)(err < -128 ? -128 : err) };
    reply(e, sizeof(e));
}

/* Names, in the order the listing gave them, so an index means the same file
 * to both ends. Rebuilt per command rather than cached: the device is still
 * recording while this runs, and a cached list goes stale in minutes. */
static int nth_name(uint16_t want, char *out, size_t out_len, size_t *size)
{
    struct fs_dir_t dir;
    fs_dir_t_init(&dir);
    if (fs_opendir(&dir, DIR) != 0) {
        return -ENOENT;
    }

    uint16_t i = 0;
    int rc = -ENOENT;
    for (;;) {
        struct fs_dirent ent;
        if (fs_readdir(&dir, &ent) != 0 || ent.name[0] == '\0') {
            break;
        }
        if (ent.type != FS_DIR_ENTRY_FILE) {
            continue;
        }
        if (i == want) {
            strncpy(out, ent.name, out_len - 1);
            out[out_len - 1] = '\0';
            if (size) {
                *size = ent.size;
            }
            rc = 0;
            break;
        }
        i++;
    }
    fs_closedir(&dir);
    return rc;
}

static void do_list(void)
{
    if (k_mutex_lock(&sd_lock, K_MSEC(2000)) != 0) {
        reply_error(-EBUSY);
        return;
    }

    /* What is still in RAM is part of the answer, or the newest recording
     * reads short to anyone who asks. Zephyr mutexes are recursive for the
     * owning thread, so this taking sd_lock again is a counter increment
     * rather than the deadlock it looks like. */
    sd_store_flush();

    struct fs_dir_t dir;
    fs_dir_t_init(&dir);
    if (fs_opendir(&dir, DIR) != 0) {
        k_mutex_unlock(&sd_lock);
        uint8_t end = FILES_END;
        reply(&end, 1);           /* no directory yet is an empty list */
        return;
    }

    uint16_t index = 0;
    for (;;) {
        struct fs_dirent ent;
        if (fs_readdir(&dir, &ent) != 0 || ent.name[0] == '\0') {
            break;
        }
        if (ent.type != FS_DIR_ENTRY_FILE) {
            continue;
        }

        size_t nlen = strlen(ent.name);
        if (nlen > CHUNK - 1) {
            nlen = CHUNK - 1;
        }
        buf[0] = FILES_ENTRY;
        buf[1] = index & 0xFF;
        buf[2] = index >> 8;
        buf[3] = (uint8_t)(ent.size & 0xFF);
        buf[4] = (uint8_t)((ent.size >> 8) & 0xFF);
        buf[5] = (uint8_t)((ent.size >> 16) & 0xFF);
        buf[6] = (uint8_t)((ent.size >> 24) & 0xFF);
        memcpy(&buf[7], ent.name, nlen);
        reply(buf, 7 + nlen);
        index++;
    }
    fs_closedir(&dir);
    k_mutex_unlock(&sd_lock);

    uint8_t end = FILES_END;
    reply(&end, 1);
    n_lists++;
}

static void do_read(uint16_t which)
{
    char name[MAX_FILE_NAME + 1];
    size_t size = 0;

    if (k_mutex_lock(&sd_lock, K_MSEC(2000)) != 0) {
        reply_error(-EBUSY);
        return;
    }
    sd_store_flush();      /* recursive lock; see do_list() */
    int rc = nth_name(which, name, sizeof(name), &size);
    k_mutex_unlock(&sd_lock);

    if (rc != 0) {
        reply_error(rc);
        return;
    }

    char path[sizeof(DIR) + 1 + MAX_FILE_NAME + 1];
    snprintf(path, sizeof(path), DIR "/%s", name);

    LOG_INF("sending %s (%u bytes)", name, (unsigned)size);

    uint16_t seq = 0;
    size_t sent = 0;

    while (!atomic_get(&abort_flag)) {
        if (!ble_audio_connected()) {
            n_aborts++;
            return;                       /* nobody is listening any more */
        }

        /* The lock is taken and released per chunk rather than held across
         * the whole file. A megabyte at this rate is four minutes, and
         * holding the card for four minutes would stop the recording that is
         * still going on behind this. */
        if (k_mutex_lock(&sd_lock, K_MSEC(2000)) != 0) {
            reply_error(-EBUSY);
            return;
        }

        struct fs_file_t f;
        fs_file_t_init(&f);
        if (fs_open(&f, path, FS_O_READ) != 0) {
            k_mutex_unlock(&sd_lock);
            reply_error(-EIO);
            return;
        }
        fs_seek(&f, sent, FS_SEEK_SET);
        ssize_t got = fs_read(&f, &buf[3], CHUNK);
        fs_close(&f);
        k_mutex_unlock(&sd_lock);

        if (got < 0) {
            reply_error(-EIO);
            return;
        }
        if (got == 0) {
            break;
        }

        buf[0] = FILES_DATA;
        buf[1] = seq & 0xFF;
        buf[2] = seq >> 8;
        reply(buf, 3 + got);

        seq++;
        sent += got;
        n_bytes += got;

        /* Yield between chunks. Live audio and the flash writer both want
         * time, and a transfer that starves them turns a convenience into a
         * dropped conversation. */
        k_msleep(2);
    }

    if (atomic_get(&abort_flag)) {
        n_aborts++;
        return;
    }

    uint8_t done[3] = { FILES_DONE, seq & 0xFF, seq >> 8 };
    reply(done, sizeof(done));
    n_reads++;
    LOG_INF("sent %u bytes in %u chunks", (unsigned)sent, seq);
}

static void xfer_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

    for (;;) {
        k_sem_take(&work, K_FOREVER);

        uint8_t cmd = (uint8_t)atomic_get(&pending_cmd);
        uint16_t arg = (uint16_t)atomic_get(&pending_arg);
        atomic_set(&abort_flag, 0);
        atomic_set(&busy, 1);

        if (sd_is_released()) {
            /* A USB host has the volume. Reading it now would be reading a
             * filesystem somebody else is editing. */
            reply_error(-EBUSY);
        } else if (!sd_mounted()) {
            reply_error(-ENODEV);
        } else {
            switch (cmd) {
            case FILES_LIST: do_list();     break;
            case FILES_READ: do_read(arg);  break;
            default:         reply_error(-EINVAL); break;
            }
        }
        atomic_set(&busy, 0);
    }
}

void sd_xfer_init(void)
{
    k_thread_create(&xfer_thread, xfer_stack, XFER_STACK, xfer_fn,
                    NULL, NULL, NULL, K_PRIO_PREEMPT(11), 0, K_NO_WAIT);
    k_thread_name_set(&xfer_thread, "sd-xfer");
}

void sd_xfer_command(const uint8_t *cmd, uint16_t len)
{
    if (len < 1) {
        return;
    }
    if (cmd[0] == FILES_STOP) {
        atomic_set(&abort_flag, 1);
        return;
    }
    atomic_set(&pending_cmd, cmd[0]);
    atomic_set(&pending_arg, len >= 3 ? (cmd[1] | (cmd[2] << 8)) : 0);
    /* Whatever is running is superseded. Someone asking for a second thing
     * has stopped wanting the first. */
    atomic_set(&abort_flag, 1);
    k_sem_give(&work);
}

void sd_xfer_abort(void) { atomic_set(&abort_flag, 1); }

bool sd_xfer_busy(void) { return atomic_get(&busy) != 0; }

void sd_xfer_stats(uint32_t *lists, uint32_t *reads, uint32_t *bytes,
                   uint32_t *aborts)
{
    if (lists)  *lists  = n_lists;
    if (reads)  *reads  = n_reads;
    if (bytes)  *bytes  = n_bytes;
    if (aborts) *aborts = n_aborts;
}

#else  /* no card slot */

void sd_xfer_init(void) { }
void sd_xfer_command(const uint8_t *cmd, uint16_t len) { (void)cmd; (void)len; }
void sd_xfer_abort(void) { }
bool sd_xfer_busy(void) { return false; }
void sd_xfer_stats(uint32_t *l, uint32_t *r, uint32_t *b, uint32_t *a)
{
    if (l) *l = 0; if (r) *r = 0; if (b) *b = 0; if (a) *a = 0;
}

#endif
