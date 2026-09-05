#ifndef BOSWELL_CLOCK_H
#define BOSWELL_CLOCK_H

#include <stdbool.h>
#include <stdint.h>

/*
 * What time it is, on a device that has no idea.
 *
 * There is no RTC here and no battery-backed anything. `k_uptime_get()` says
 * how long since boot and nothing more, which is why a recording made at 3
 * a.m. on a walk could only report that it happened 41 minutes in. That is
 * fine while a host is always attached and stamps arrival time; it stops
 * being fine the moment the device records alone, which is the whole point of
 * the card.
 *
 * So the host tells it. On connect it sends CTRL_SET_TIME with the current
 * epoch, the device works out epoch-minus-uptime, and every timestamp after
 * that is that offset plus uptime. The offset is deliberately *not*
 * persisted: a device that has been off for a week would otherwise wake up
 * confidently wrong, and being honestly unset is far more useful than being
 * plausibly incorrect -- an unset clock can be repaired at ingest from the
 * file's position in the sequence, while a wrong one cannot be detected at
 * all.
 */

/* Adopt the host's idea of the time. Seconds since the Unix epoch. */
void clock_set_epoch(uint32_t epoch);

/* True once a host has said. */
bool clock_is_set(void);

/* Seconds since the epoch, or 0 if no host has ever said. */
uint32_t clock_now(void);

/* What clock_now() would have returned at this device uptime, so a frame
 * captured earlier can be placed correctly rather than stamped on arrival.
 * 0 when the clock is unset. */
uint32_t clock_at_uptime(uint32_t uptime_ms);

/* The epoch that corresponds to uptime zero, or 0 if unset.
 *
 * This, not "now", is what a recording needs to carry. It is constant for the
 * whole run, so a file that stores it lets any frame in it be placed from its
 * own device_ms alone -- and a frame's device_ms is the one number that is
 * certainly right, because the same counter stamped every copy of it. */
uint32_t clock_boot_epoch(void);

#endif /* BOSWELL_CLOCK_H */
