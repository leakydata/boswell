#ifndef BOSWELL_CODEC_H
#define BOSWELL_CODEC_H

#include "proto.h"

/* Encode one frame of PCM into the shared wire format.
 * Returns the number of bytes written to `out`. */
uint16_t codec_build_frame(const int16_t *samples, int count, uint16_t seq,
                           uint32_t t_ms, uint8_t flags, uint8_t *out);

/* Root-mean-square of a frame, used by the energy gate. */
uint32_t codec_rms(const int16_t *samples, int count);

#ifdef CONFIG_BOSWELL_OPUS
/* What the encoder needs, and what has been set aside for it. Two numbers
 * rather than a boolean, because the interesting question is not "does it
 * fit" but "by how much" -- the reserve was picked to be comfortably large
 * and has never been measured against the real figure. */
int codec_opus_state_bytes(void);
int codec_opus_reserved(void);
#endif

#endif
