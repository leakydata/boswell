#pragma once

#include <stdbool.h>
#include <stdint.h>

struct shell;

/* Reports card state to the shell. 0 when the card round-trips a file. */
int sd_probe(const struct shell *sh);

/* Mount the card, off the boot path.
 *
 * Mounting a 16 GB card took sixteen seconds on this hardware, most of it
 * counting free clusters, so doing it inline would hold up capture for the
 * length of a conversation. This queues the work and returns.
 */
void sd_probe_init(void);

struct sd_status {
	bool     mounted;
	uint16_t total_mb;
	uint16_t free_mb;
};

/* Cheap: the last known state, not a fresh measurement. Safe to call from
 * the info characteristic, which is read about once a second. */
void sd_status_get(struct sd_status *out);

/* Refresh the cached free space if it is stale. Counting free clusters walks
 * the whole allocation table, so this is rate-limited internally and is not
 * something to call per frame. */
void sd_status_poll(void);
