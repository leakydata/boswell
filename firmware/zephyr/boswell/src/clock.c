#include "clock.h"

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(boswell_clock, LOG_LEVEL_INF);

/* Seconds to add to uptime-in-seconds to get the epoch. Zero means nobody
 * has said, which is a state worth keeping distinguishable rather than
 * papering over with a plausible default. */
static uint32_t epoch_offset;
static bool     have_time;

void clock_set_epoch(uint32_t epoch)
{
    uint32_t up = (uint32_t)(k_uptime_get() / 1000);

    if (epoch < 1700000000u) {
        /* Before late 2023. A host with a dead RTC or a clock still at the
         * epoch would otherwise stamp a day of recordings as 1970, which
         * reads as data rather than as a fault. */
        LOG_WRN("refusing implausible epoch %u", epoch);
        return;
    }

    uint32_t was = epoch_offset;
    epoch_offset = epoch - up;

    if (!have_time) {
        have_time = true;
        LOG_INF("clock set: epoch %u at uptime %u s", epoch, up);
    } else {
        int32_t drift = (int32_t)(epoch_offset - was);
        if (drift > 5 || drift < -5) {
            /* Worth saying out loud. The offset should be near-constant, so
             * a jump means either the host's clock moved or the device's
             * uptime did, and both change how earlier recordings should be
             * read. */
            LOG_INF("clock adjusted by %d s", drift);
        }
    }
}

bool clock_is_set(void) { return have_time; }

uint32_t clock_boot_epoch(void) { return have_time ? epoch_offset : 0; }

uint32_t clock_now(void)
{
    if (!have_time) {
        return 0;
    }
    return epoch_offset + (uint32_t)(k_uptime_get() / 1000);
}

uint32_t clock_at_uptime(uint32_t uptime_ms)
{
    if (!have_time) {
        return 0;
    }
    return epoch_offset + uptime_ms / 1000;
}
