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
#define BOSWELL_SETTINGS_VER   1

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
    uint8_t  mic_power_save;
    uint8_t  tap_thresh;
    uint16_t tap_debounce_ms;
    int8_t   tx_power;
};

int  cfg_store_init(void);
bool cfg_store_load(struct boswell_settings *out);
/* Mark dirty; the actual write happens a few seconds later. */
void cfg_store_touch(void);
/* Call regularly from the housekeeping loop. */
void cfg_store_service(const struct boswell_settings *cur);
bool cfg_store_ready(void);

#endif
