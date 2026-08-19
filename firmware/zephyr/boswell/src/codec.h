#ifndef BOSWELL_CODEC_H
#define BOSWELL_CODEC_H

#include "proto.h"

/* Encode one frame of PCM into the shared wire format.
 * Returns the number of bytes written to `out`. */
uint16_t codec_build_frame(const int16_t *samples, int count, uint16_t seq,
                           uint32_t t_ms, uint8_t flags, uint8_t *out);

/* Root-mean-square of a frame, used by the energy gate. */
uint32_t codec_rms(const int16_t *samples, int count);

#endif
