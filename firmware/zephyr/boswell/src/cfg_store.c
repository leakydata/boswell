#include "cfg_store.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/fs/nvs.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(cfg_store, LOG_LEVEL_INF);

#define NVS_PARTITION      storage_partition
#define NVS_PARTITION_DEV  FIXED_PARTITION_DEVICE(NVS_PARTITION)
#define NVS_PARTITION_OFF  FIXED_PARTITION_OFFSET(NVS_PARTITION)
#define NVS_PARTITION_SIZE FIXED_PARTITION_SIZE(NVS_PARTITION)

#define SETTINGS_ID   1
#define BACKLOG_ID    2
#define SAVE_DELAY_MS 3000

static struct nvs_fs fs;
static bool    ready;
static int64_t dirty_at;

int cfg_store_init(void)
{
    struct flash_pages_info info;

    fs.flash_device = NVS_PARTITION_DEV;
    if (!device_is_ready(fs.flash_device)) {
        LOG_ERR("settings flash not ready");
        return -ENODEV;
    }
    fs.offset = NVS_PARTITION_OFF;

    int err = flash_get_page_info_by_offs(fs.flash_device, fs.offset, &info);
    if (err) {
        LOG_ERR("page info failed (%d)", err);
        return err;
    }
    fs.sector_size  = info.size;
    fs.sector_count = NVS_PARTITION_SIZE / info.size;

    err = nvs_mount(&fs);
    if (err) {
        LOG_ERR("nvs_mount failed (%d)", err);
        return err;
    }
    ready = true;
    LOG_INF("settings: %u sectors of %u B", fs.sector_count, fs.sector_size);
    return 0;
}

bool cfg_store_ready(void) { return ready; }

bool cfg_store_load(struct boswell_settings *out)
{
    if (!ready || out == NULL) {
        return false;
    }
    ssize_t n = nvs_read(&fs, SETTINGS_ID, out, sizeof(*out));

    if (n != (ssize_t)sizeof(*out)) {
        LOG_INF("no saved settings, using defaults");
        return false;
    }
    if (out->magic != BOSWELL_SETTINGS_MAGIC ||
        out->version != BOSWELL_SETTINGS_VER) {
        LOG_WRN("settings magic/version mismatch, using defaults");
        return false;
    }
    LOG_INF("settings restored");
    return true;
}

bool cfg_store_load_backlog(struct boswell_backlog *out)
{
    if (!ready || out == NULL) {
        return false;
    }
    ssize_t n = nvs_read(&fs, BACKLOG_ID, out, sizeof(*out));

    if (n != (ssize_t)sizeof(*out)) {
        return false;
    }
    if (out->magic != BOSWELL_BACKLOG_MAGIC ||
        out->version != BOSWELL_BACKLOG_VER) {
        LOG_WRN("backlog cursor magic/version mismatch, ignoring");
        return false;
    }
    return true;
}

void cfg_store_save_backlog(int64_t w_pos, int64_t r_pos)
{
    if (!ready) {
        return;
    }
    struct boswell_backlog rec = {
        .magic = BOSWELL_BACKLOG_MAGIC,
        .version = BOSWELL_BACKLOG_VER,
        .w_pos = w_pos,
        .r_pos = r_pos,
    };
    /* NVS skips a write whose contents match what is stored, so calling this
     * with unchanged cursors costs nothing. */
    ssize_t n = nvs_write(&fs, BACKLOG_ID, &rec, sizeof(rec));

    if (n < 0) {
        LOG_ERR("backlog cursor save failed (%d)", (int)n);
    }
}

void cfg_store_touch(void)
{
    dirty_at = k_uptime_get();
}

void cfg_store_service(const struct boswell_settings *cur)
{
    if (!ready || dirty_at == 0 || cur == NULL) {
        return;
    }
    if (k_uptime_get() - dirty_at < SAVE_DELAY_MS) {
        return;
    }
    dirty_at = 0;

    struct boswell_settings rec = *cur;
    rec.magic   = BOSWELL_SETTINGS_MAGIC;
    rec.version = BOSWELL_SETTINGS_VER;

    /* NVS already skips a write whose contents match the stored record, so
     * repeatedly settling on the same value does not consume flash. */
    ssize_t n = nvs_write(&fs, SETTINGS_ID, &rec, sizeof(rec));
    if (n < 0) {
        LOG_ERR("settings save failed (%d)", (int)n);
    } else {
        LOG_INF("settings saved");
    }
}
