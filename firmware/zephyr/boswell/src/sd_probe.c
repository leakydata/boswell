/*
 * Is the card there, and does it work?
 *
 * Reporting "mounted" is not the same as reporting "usable". A card can
 * enumerate, report a sensible capacity, and still fail every write -- a
 * marginal solder joint on MISO looks exactly like a healthy card until
 * something reads back what it wrote. So this probes in four steps and says
 * which one failed, because each points somewhere different:
 *
 *   disk init    -> wiring, CS, or no card in the slot
 *   capacity     -> the card answered CSD; SPI is working in both directions
 *   mount        -> the medium is fine, the filesystem on it is not
 *   round trip   -> data written is data read back
 */

#include "sd_probe.h"

#include <zephyr/fs/fs.h>
#include <zephyr/storage/disk_access.h>
#include <zephyr/shell/shell.h>
#include <ff.h>

#include <string.h>

#define DISK "SD"
#define MOUNT "/SD:"
#define PROBE_FILE MOUNT "/boswell_probe.txt"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

/* Not "sd": Zephyr's own subsys/sd registers that name, and two modules with
 * one name is a link error about log_const_sd that says nothing about logging. */
LOG_MODULE_REGISTER(boswell_sd, LOG_LEVEL_INF);

static FATFS fat;
static struct fs_mount_t mp = {
    .type = FS_FATFS,
    .fs_data = &fat,
    .mnt_point = MOUNT,
};
static bool mounted;

/* Cached, because measuring is expensive.
 *
 * fs_statvfs() on FAT counts free clusters by walking the allocation table.
 * On this card that is seconds, and the info characteristic is read about
 * once a second -- so measuring on demand would put a multi-second blocking
 * SPI transfer on the path that reports battery and capture state. The
 * numbers move slowly; a stale free-space figure is worth far more than a
 * status characteristic that stalls.
 */
#define STATS_STALE_MS (5 * 60 * 1000)

/* The card gets its own thread, and nothing else may wait on it.
 *
 * This cost a watchdog reset to learn. Mounting takes about sixteen seconds
 * on this card and counting free clusters takes seconds more, and both were
 * reachable from ble_audio_publish_info() -- which runs on the Bluetooth RX
 * thread and on the system workqueue. A multi-second block there stalls the
 * transmit path, so WDT_TX never checks in, and thirty seconds later the
 * watchdog resets the SoC. The board came up reporting
 * "last reset=0x00000002 watchdog" and nothing else looked wrong.
 *
 * The system workqueue was the wrong home for the same reason: it is shared
 * with tap handling and with connection callbacks, and a card that decides
 * to spend twenty seconds on internal housekeeping must not be able to hold
 * any of that up. A dedicated queue at a preemptible priority means the
 * worst a slow card can do is report stale numbers.
 */
#define SD_WQ_STACK 4096
#define SD_WQ_PRIO  K_PRIO_PREEMPT(10)

K_THREAD_STACK_DEFINE(sd_wq_stack, SD_WQ_STACK);

static struct k_work_q  sd_wq;
/* The shell can probe the card while the queue is measuring it. FATFS is not
 * reentrant across threads for the same volume, and two walks of the same
 * allocation table would be a corrupt reading at best. */
K_MUTEX_DEFINE(sd_lock);
static struct sd_status cached;
static int64_t          cached_at;
static struct k_work    mount_work;

static int do_mount(void)
{
    if (mounted) {
        return 0;
    }
    /* Logged step by step because the failure being chased is a hang, not an
     * error code: a mount that never returns leaves no return value to
     * inspect, and the last line printed is the only thing that says which
     * call is stuck. */
    LOG_INF("mount: disk init");
    int64_t t0 = k_uptime_get();
    if (disk_access_init(DISK) != 0) {
        LOG_WRN("mount: no card (%lld ms)", k_uptime_get() - t0);
        return -ENODEV;
    }
    LOG_INF("mount: disk ready in %lld ms, mounting", k_uptime_get() - t0);
    t0 = k_uptime_get();
    int err = fs_mount(&mp);
    if (err != 0) {
        LOG_WRN("mount: fs_mount %d after %lld ms", err, k_uptime_get() - t0);
        return err;
    }
    LOG_INF("mount: mounted in %lld ms", k_uptime_get() - t0);
    mounted = true;
    return 0;
}

static void measure(void)
{
    struct fs_statvfs st;

    cached_at = k_uptime_get();
    int64_t t0 = k_uptime_get();
    LOG_INF("stat: counting free clusters");
    if (!mounted || fs_statvfs(MOUNT, &st) != 0) {
        LOG_WRN("stat: statvfs failed after %lld ms", k_uptime_get() - t0);
        cached.mounted = mounted;
        return;
    }
    LOG_INF("stat: took %lld ms", k_uptime_get() - t0);
    LOG_INF("stat: %llu blocks free of %llu", (uint64_t) st.f_bfree,
            (uint64_t) st.f_blocks);
    uint64_t all_mb  = ((uint64_t) st.f_blocks * st.f_frsize) / (1024ULL * 1024ULL);
    uint64_t free_mb = ((uint64_t) st.f_bfree  * st.f_frsize) / (1024ULL * 1024ULL);

    /* Published as 16-bit megabytes, which tops out at 64 GB. A larger card
     * would wrap and report a small one, so it saturates instead: a figure
     * that is merely capped is recoverable, one that wrapped is a lie. */
    cached.mounted  = true;
    cached.total_mb = all_mb  > 0xFFFF ? 0xFFFF : (uint16_t) all_mb;
    cached.free_mb  = free_mb > 0xFFFF ? 0xFFFF : (uint16_t) free_mb;
}

static void mount_work_fn(struct k_work *w)
{
    ARG_UNUSED(w);
    k_mutex_lock(&sd_lock, K_FOREVER);
    if (do_mount() == 0) {
        measure();
    }
    k_mutex_unlock(&sd_lock);
}

void sd_probe_init(void)
{
    k_work_queue_start(&sd_wq, sd_wq_stack, K_THREAD_STACK_SIZEOF(sd_wq_stack),
                       SD_WQ_PRIO, NULL);
    k_thread_name_set(&sd_wq.thread, "sd");
    k_work_init(&mount_work, mount_work_fn);
    k_work_submit_to_queue(&sd_wq, &mount_work);
}

void sd_status_get(struct sd_status *out)
{
    if (out) {
        *out = cached;
    }
}

void sd_status_poll(void)
{
    /* Asks; does not measure. Every caller of this is on a path that must
     * not block -- the info characteristic is published from the Bluetooth
     * RX thread -- so the work is handed to the card's own queue and the
     * caller returns with whatever the last measurement said.
     *
     * cached_at moves before the work runs rather than after, so a poll a
     * second later does not queue the same measurement again while the
     * first is still walking the allocation table.
     */
    if (!mounted) {
        return;
    }
    if (cached_at != 0 && k_uptime_get() - cached_at < STATS_STALE_MS) {
        return;
    }
    cached_at = k_uptime_get();
    k_work_submit_to_queue(&sd_wq, &mount_work);
}

static int sd_probe_locked(const struct shell *sh)
{
    uint32_t sector_count = 0, sector_size = 0;
    int err;

    /* Init is the step that fails when there is no card, so say so plainly
     * rather than leaving a driver error code to be looked up. */
    err = disk_access_init(DISK);
    if (err != 0) {
        shell_print(sh, "sd: no card (disk init %d)", err);
        shell_print(sh, "    check the card is seated, and CS on D0/P0.02");
        return err;
    }

    if (disk_access_ioctl(DISK, DISK_IOCTL_GET_SECTOR_COUNT, &sector_count) != 0 ||
        disk_access_ioctl(DISK, DISK_IOCTL_GET_SECTOR_SIZE, &sector_size) != 0) {
        shell_print(sh, "sd: card answered init but not its geometry");
        return -EIO;
    }

    /* 64-bit: 16 GB in bytes overflows a uint32_t, and reporting a wrapped
     * capacity would look like a much smaller card. */
    uint64_t bytes = (uint64_t) sector_count * sector_size;
    shell_print(sh, "sd: %u sectors x %u B = %llu MB",
                sector_count, sector_size, bytes / (1024ULL * 1024ULL));

    err = do_mount();
    if (err != 0) {
        shell_print(sh, "sd: card is readable but will not mount (%d)", err);
        return err;
    }

    struct fs_statvfs st;
    if (fs_statvfs(MOUNT, &st) == 0) {
        uint64_t free_b = (uint64_t) st.f_bfree * st.f_frsize;
        uint64_t all_b  = (uint64_t) st.f_blocks * st.f_frsize;
        shell_print(sh, "sd: mounted %s, %llu MB free of %llu MB",
                    MOUNT, free_b / (1024ULL * 1024ULL), all_b / (1024ULL * 1024ULL));
    }

    /* The step that actually proves it. */
    static const char msg[] = "boswell card probe";
    char back[sizeof(msg)] = {0};
    struct fs_file_t f;

    fs_file_t_init(&f);
    err = fs_open(&f, PROBE_FILE, FS_O_CREATE | FS_O_WRITE | FS_O_TRUNC);
    if (err != 0) {
        shell_print(sh, "sd: mounted but cannot create a file (%d)", err);
        return err;
    }
    ssize_t wrote = fs_write(&f, msg, sizeof(msg));
    fs_close(&f);
    if (wrote != (ssize_t) sizeof(msg)) {
        shell_print(sh, "sd: short write (%d of %u)", (int) wrote, (unsigned) sizeof(msg));
        return -EIO;
    }

    fs_file_t_init(&f);
    err = fs_open(&f, PROBE_FILE, FS_O_READ);
    if (err != 0) {
        shell_print(sh, "sd: wrote a file it cannot reopen (%d)", err);
        return err;
    }
    ssize_t got = fs_read(&f, back, sizeof(back));
    fs_close(&f);

    if (got != (ssize_t) sizeof(msg) || memcmp(msg, back, sizeof(msg)) != 0) {
        shell_print(sh, "sd: READ BACK DID NOT MATCH -- the card is not trustworthy");
        return -EIO;
    }

    /* Leave nothing behind; a stray file in the root is how a probe becomes
     * a thing somebody has to clean up later. */
    fs_unlink(PROBE_FILE);

    /* The probe just changed the free space, and it is the one moment the
     * cost of counting clusters is already being paid for. */
    measure();

    shell_print(sh, "sd: round trip ok -- card is working");
    return 0;
}

int sd_probe(const struct shell *sh)
{
    /* Held for the whole probe, not per step: the write and the read back
     * are one measurement, and a refresh landing between them would be
     * measuring a different card state than the one being reported. */
    /* Bounded, not K_FOREVER.
     *
     * This command exists to diagnose the card, and the state it most needs
     * to report is the one where the card is stuck -- which is exactly when
     * a K_FOREVER lock makes it hang with no output at all. A diagnostic that
     * goes silent when the fault appears is worse than none, because the
     * silence gets read as a dead board rather than a busy one.
     */
    if (k_mutex_lock(&sd_lock, K_SECONDS(5)) != 0) {
        shell_print(sh, "sd: busy -- a mount or refresh has been running for "
                        "over 5 s and has not returned");
        shell_print(sh, "    that is the fault, not a timeout of this command");
        return -EBUSY;
    }
    int rc = sd_probe_locked(sh);
    k_mutex_unlock(&sd_lock);
    return rc;
}
