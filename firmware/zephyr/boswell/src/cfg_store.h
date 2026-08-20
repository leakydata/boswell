/*
 * Persisted settings, in the board's 32 KB storage partition via NVS.
 *
 * Saves are debounced: a slider dragged across the web UI produces a stream
 * of control writes, and committing each one would burn flash cycles for
 * values the user is still in the middle of choosing.
 *
 * The struct is versioned. A record whose magic or version does not match is
 * ignored rather than reinterpreted, so a firmware update that changes the
 * layout falls back to defaults instead of loading garbage as configuration.
 */

#ifndef BOSWELL_CFG_STORE_H
#define BOSWELL_CFG_STORE_H

#include <stdbool.h>
#include <stdint.h>

#define BOSWELL_SETTINGS_MAGIC 0xB05E
/* 2: buffering split out from backlog_mode, which now means replay order
   only. A version 1 record describes a field that meant both. */
#define BOSWELL_SETTINGS_VER   2

struct boswell_settings {
    uint16_t magic;
    uint8_t  version;
    uint8_t  gain;
    uint8_t  use16k;
    uint8_t  vad_enabled;
    uint16_t vad_thresh;
    uint8_t  led_level;
    uint8_t  led_mode;
    uint8_t  backlog_mode;
    uint8_t  buffering;
    uint8_t  mic_power_save;
    uint8_t  tap_thresh;
    uint16_t tap_debounce_ms;
    int8_t   tx_power;
};

/* Where the flash backlog had got to.
 *
 * Buffered audio survives a reboot physically -- it is in external flash --
 * but the cursors that say which of it is unsent were reset to zero on every
 * boot, so a watchdog reset or a battery interruption discarded everything
 * waiting. That is a hole in the one guarantee the store exists to make.
 *
 * Positions are the monotonic counters the ring uses, not addresses, so they
 * survive the modulo arithmetic unchanged. Validated on load against the
 * actual flash contents before being trusted.
 */
#define BOSWELL_BACKLOG_MAGIC 0xB0C5
/* 2: records gained a CRC byte, so a version 1 cursor describes a layout
   that no longer exists and must not be trusted. */
#define BOSWELL_BACKLOG_VER   2

struct boswell_backlog {
    uint16_t magic;
    uint8_t  version;
    uint8_t  _pad;
    int64_t  w_pos;
    int64_t  r_pos;
};

int  cfg_store_init(void);
bool cfg_store_load_backlog(struct boswell_backlog *out);
void cfg_store_save_backlog(int64_t w_pos, int64_t r_pos);
bool cfg_store_load(struct boswell_settings *out);
/* Mark dirty; the actual write happens a few seconds later. */
void cfg_store_touch(void);
/* Call regularly from the housekeeping loop. */
void cfg_store_service(const struct boswell_settings *cur);
bool cfg_store_ready(void);

#endif
