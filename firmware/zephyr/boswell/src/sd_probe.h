#pragma once

#include <stdbool.h>
#include <stdint.h>

#include <zephyr/kernel.h>

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

/* The card's mount point, and the lock that serialises every use of it.
 *
 * CONFIG_FS_FATFS_REENTRANT is not set, so FATFS has no internal locking and
 * two threads inside it at once corrupts the volume. Everything that touches
 * the filesystem takes this -- the mount, the free-space walk, and the audio
 * writer.
 *
 * Take it with a timeout, never K_FOREVER, from anything on the audio path:
 * the first mount of this card took sixteen seconds, and a writer that waits
 * that long is a writer that has stopped feeding its watchdog.
 */
#define SD_MOUNT_POINT "/SD:"
extern struct k_mutex sd_lock;

/* Is the volume mounted right now? */
bool sd_mounted(void);

/* Hand the card to the USB host, and take it back.
 *
 * Mass storage talks to the block device directly, sector by sector. It knows
 * nothing about the filesystem, so while the host has the volume, FATFS must
 * not merely be idle -- it must be unmounted. FATFS caches the FSINFO free
 * count and directory entries, and a host writing sectors underneath a
 * mounted volume leaves those caches describing a card that no longer exists.
 * The next firmware write then commits them, and the corruption is the
 * firmware's, not the host's.
 *
 * Both are asynchronous: unmounting flushes, and mounting this card took
 * sixteen seconds the first time, so neither can happen on a USB callback.
 */
void sd_release(void);
void sd_reclaim(void);

/* True while the host owns the card and the firmware must keep off it. */
bool sd_is_released(void);

