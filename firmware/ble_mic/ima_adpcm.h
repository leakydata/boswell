/*
 * IMA ADPCM encoder — 4 bits per sample, 4:1 compression.
 *
 * Each BLE frame carries its own predictor + step index, so a dropped frame
 * costs you that frame and nothing else. A stream-wide encoder state would
 * desynchronise the decoder permanently on the first lost packet, which over
 * a flaky radio link is not an acceptable failure mode.
 */

#ifndef IMA_ADPCM_H
#define IMA_ADPCM_H

#include <stdint.h>

static const int8_t kIndexTable[16] = {
  -1, -1, -1, -1, 2, 4, 6, 8,
  -1, -1, -1, -1, 2, 4, 6, 8
};

static const int16_t kStepTable[89] = {
      7,     8,     9,    10,    11,    12,    13,    14,    16,    17,
     19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
     50,    55,    60,    66,    73,    80,    88,    97,   107,   118,
    130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
    337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
    876,   963,  1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
   2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
   5894,  6484,  7132,  7845,  8630,  9493, 10442, 11487, 12635, 13899,
  15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767
};

struct AdpcmState {
  int32_t predictor;
  int8_t  index;
};

/* Encode one sample to a 4-bit code, advancing the state exactly as the
 * decoder will, so encoder and decoder stay in lockstep. */
static inline uint8_t adpcm_encode_sample(int16_t sample, AdpcmState *st) {
  int32_t step = kStepTable[st->index];
  int32_t diff = (int32_t)sample - st->predictor;

  uint8_t code = 0;
  if (diff < 0) {
    code = 8;
    diff = -diff;
  }

  int32_t tmp = step;
  if (diff >= tmp) { code |= 4; diff -= tmp; }
  tmp >>= 1;
  if (diff >= tmp) { code |= 2; diff -= tmp; }
  tmp >>= 1;
  if (diff >= tmp) { code |= 1; }

  /* Reconstruct with the decoder's arithmetic. */
  int32_t diffq = step >> 3;
  if (code & 4) diffq += step;
  if (code & 2) diffq += step >> 1;
  if (code & 1) diffq += step >> 2;

  if (code & 8) st->predictor -= diffq;
  else          st->predictor += diffq;

  if (st->predictor > 32767)       st->predictor = 32767;
  else if (st->predictor < -32768) st->predictor = -32768;

  st->index += kIndexTable[code];
  if (st->index < 0)       st->index = 0;
  else if (st->index > 88) st->index = 88;

  return code;
}

/* Pack `count` samples into `count/2` bytes, low nibble first. */
static inline void adpcm_encode_block(const int16_t *samples, int count,
                                      uint8_t *out, AdpcmState *st) {
  for (int i = 0; i < count; i += 2) {
    uint8_t lo = adpcm_encode_sample(samples[i], st);
    uint8_t hi = adpcm_encode_sample(samples[i + 1], st);
    out[i >> 1] = (uint8_t)(lo | (hi << 4));
  }
}

#endif
