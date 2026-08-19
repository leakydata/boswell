#include "codec.h"
#include "ima_adpcm.h"
#include <zephyr/sys/util.h>

uint32_t codec_rms(const int16_t *samples, int count)
{
    uint64_t acc = 0;
    for (int i = 0; i < count; i++) {
        int32_t v = samples[i];
        acc += (uint64_t)(v * v);
    }
    if (count == 0) {
        return 0;
    }
    /* Integer square root: no FPU work on the audio path. */
    uint64_t mean = acc / count;
    uint32_t r = 0, bit = 1u << 30;
    while (bit > mean) {
        bit >>= 2;
    }
    while (bit) {
        if (mean >= (uint64_t)r + bit) {
            mean -= r + bit;
            r = (r >> 1) + bit;
        } else {
            r >>= 1;
        }
        bit >>= 2;
    }
    return r;
}

uint16_t codec_build_frame(const int16_t *samples, int count, uint16_t seq,
                           uint32_t t_ms, uint8_t flags, uint8_t *out)
{
    struct AdpcmState st = { samples[0], 0 };
    int16_t predictor0 = (int16_t)st.predictor;
    uint8_t index0 = (uint8_t)st.index;

    adpcm_encode_block(samples, count, out + PROTO_HEADER_LEN, &st);

    out[0] = seq & 0xFF;
    out[1] = (seq >> 8) & 0xFF;
    out[2] = flags;
    out[3] = index0;
    out[4] = predictor0 & 0xFF;
    out[5] = (predictor0 >> 8) & 0xFF;
    out[6] = count & 0xFF;
    out[7] = (count >> 8) & 0xFF;
    out[8]  = (uint8_t)(t_ms & 0xFF);
    out[9]  = (uint8_t)((t_ms >> 8) & 0xFF);
    out[10] = (uint8_t)((t_ms >> 16) & 0xFF);
    out[11] = (uint8_t)((t_ms >> 24) & 0xFF);

    return (uint16_t)(PROTO_HEADER_LEN + count / 2);
}
