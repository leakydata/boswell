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

static struct sd_status cached;
static int64_t          cached_at;
static struct k_work    mount_work;

static int do_mount(void)
{
    if (mounted) {
        return 0;
    }
    if (disk_access_init(DISK) != 0) {
        return -ENODEV;
    }
    int err = fs_mount(&mp);
    if (err != 0) {
        return err;
    }
    mounted = true;
    return 0;
}

static void measure(void)
{
    struct fs_statvfs st;

    cached_at = k_uptime_get();
    if (!mounted || fs_statvfs(MOUNT, &st) != 0) {
        cached.mounted = mounted;
        return;
    }
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
    if (do_mount() == 0) {
        measure();
    }
}

void sd_probe_init(void)
{
    k_work_init(&mount_work, mount_work_fn);
    k_work_submit(&mount_work);
}

void sd_status_get(struct sd_status *out)
{
    if (out) {
        *out = cached;
    }
}

void sd_status_poll(void)
{
    if (!mounted) {
        return;
    }
    if (cached_at != 0 && k_uptime_get() - cached_at < STATS_STALE_MS) {
        return;
    }
    measure();
}

int sd_probe(const struct shell *sh)
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
