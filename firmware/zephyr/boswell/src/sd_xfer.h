#ifndef BOSWELL_SD_XFER_H
#define BOSWELL_SD_XFER_H

#include <stdbool.h>
#include <stdint.h>

/*
 * Reading the card's recordings out over Bluetooth.
 *
 * The cable already works and is twenty times faster, so this is a
 * convenience rather than a capability -- it is for the moment you want one
 * conversation from this morning and the device is on your chest rather than
 * on the desk.
 *
 * Everything happens on this module's own thread. Listing a directory and
 * reading a megabyte off a card are both slow, and neither may happen on the
 * Bluetooth RX thread that delivers the command: that thread also carries the
 * live audio's acknowledgements, and blocking it stops the recording that is
 * the reason the device exists.
 */

/* Start the worker. Safe on a board with no card. */
void sd_xfer_init(void);

/* Hand over a command written to the files characteristic.
 * Called from the Bluetooth RX thread; returns immediately. */
void sd_xfer_command(const uint8_t *cmd, uint16_t len);

/* Give up on whatever is in flight -- called when the link drops, because a
 * transfer with nobody listening is just a card being read for nothing. */
void sd_xfer_abort(void);

/* Is a transfer running? Reported in the shell so a slow card or a stalled
 * radio is visible rather than guessed at. */
bool sd_xfer_busy(void);

void sd_xfer_stats(uint32_t *lists, uint32_t *reads, uint32_t *bytes,
                   uint32_t *aborts);

#endif /* BOSWELL_SD_XFER_H */
