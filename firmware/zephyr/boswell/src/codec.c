#include "codec.h"
#include "ima_adpcm.h"
#include <zephyr/sys/util.h>

#ifdef CONFIG_BOSWELL_OPUS
#include <zephyr/logging/log.h>
#include <opus.h>

LOG_MODULE_REGISTER(codec, LOG_LEVEL_INF);

/*
 * Static encoder state, not the heap.
 *
 * opus_encoder_create() mallocs, and this firmware has no heap worth the
 * name -- a codec that fails to allocate mid-session would be a silent
 * capture stop, which is the failure this project keeps finding in other
 * forms. opus_encoder_init() writes into memory the caller owns, so the
 * allocation happens once, at link time, and either fits or does not.
 *
 * The size is a runtime property of how the library was configured, so it
 * cannot size this array at compile time. 20 KB is comfortably above what a
 * mono CELT-only encoder needs; the check below is what makes that a fact
 * rather than a hope.
 */
static uint8_t      enc_mem[20 * 1024] __aligned(4);
static OpusEncoder *enc;
static int          enc_rate;

static bool opus_ready(int rate)
{
    if (enc != NULL && enc_rate == rate) {
        return true;
    }

    int need = opus_encoder_get_size(1);
    if (need <= 0 || need > (int) sizeof(enc_mem)) {
        LOG_ERR("opus encoder wants %d bytes, has %u", need,
                (unsigned) sizeof(enc_mem));
        enc = NULL;
        return false;
    }

    OpusEncoder *e = (OpusEncoder *) enc_mem;
    int err = opus_encoder_init(e, rate, 1, OPUS_APPLICATION_RESTRICTED_LOWDELAY);
    if (err != OPUS_OK) {
        LOG_ERR("opus_encoder_init(%d Hz) failed: %d", rate, err);
        enc = NULL;
        return false;
    }

    /* Restricted low delay drops the SILK layer and the lookahead, which is
     * what makes 20 ms frames cost 20 ms rather than 20 plus an algorithmic
     * delay nobody asked for. Complexity 3 is Omi's setting, measured on this
     * part; the encoder runs on the capture thread and a frame that misses
     * its slot is a dropped frame, so this is not the place to buy quality
     * with cycles. */
    opus_encoder_ctl(e, OPUS_SET_BITRATE(32000));
    opus_encoder_ctl(e, OPUS_SET_COMPLEXITY(3));
    opus_encoder_ctl(e, OPUS_SET_VBR(1));
    opus_encoder_ctl(e, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));

    enc = e;
    enc_rate = rate;
    LOG_INF("opus encoder ready at %d Hz (%d bytes)", rate, need);
    return true;
}
#endif /* CONFIG_BOSWELL_OPUS */

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
    /* The encoder dereferences samples[0] and then walks pairs, so a null
     * pointer, a zero count or an odd one are all out-of-bounds reads that
     * happen to work most of the time. Every caller today satisfies these,
     * which is exactly why they were never written down -- and why a new
     * caller would find out the hard way, on a device with no debugger
     * attached. Returning zero costs one frame; the alternative is a fault
     * with nothing to point at.
     */
    if (samples == NULL || out == NULL || count <= 0 || (count & 1) ||
        count > MAX_SAMPLES) {
        return 0;
    }

#ifdef CONFIG_BOSWELL_OPUS
    {
        int rate = (flags & FLAG_16K) ? 16000 : 8000;

        /* Opus encodes whole frames of particular lengths, not arbitrary
         * runs of samples. Capture only ever produces 20 ms -- 320 at 16 kHz,
         * 160 at 8 -- but a short read upstream would arrive here as some
         * other count, and opus_encode() would reject it one layer further
         * down where the failure says less. Refuse it here, where the caller
         * already counts refusals. */
        if (count != rate / 1000 * PROTO_FRAME_MS) {
            return 0;
        }
        if (!opus_ready(rate)) {
            return 0;
        }

        int n = opus_encode(enc, samples, count, out + PROTO_HEADER_LEN,
                            MAX_FRAME_LEN - PROTO_HEADER_LEN);
        if (n <= 0) {
            return 0;
        }

        /* Bytes 3..5 are the ADPCM decoder's starting state and mean nothing
         * here. Zero rather than left alone: the caller's buffer is reused
         * frame to frame, and stale predictor bytes would look like real
         * state to anything that read them without checking the flag. */
        out[0] = seq & 0xFF;
        out[1] = (seq >> 8) & 0xFF;
        out[2] = flags | FLAG_OPUS;
        out[3] = 0;
        out[4] = 0;
        out[5] = 0;
        out[6] = count & 0xFF;
        out[7] = (count >> 8) & 0xFF;
        out[8]  = (uint8_t)(t_ms & 0xFF);
        out[9]  = (uint8_t)((t_ms >> 8) & 0xFF);
        out[10] = (uint8_t)((t_ms >> 16) & 0xFF);
        out[11] = (uint8_t)((t_ms >> 24) & 0xFF);

        return (uint16_t)(PROTO_HEADER_LEN + n);
    }
#else
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
#endif /* CONFIG_BOSWELL_OPUS */
}

#ifdef CONFIG_BOSWELL_OPUS
int codec_opus_state_bytes(void) { return opus_encoder_get_size(1); }
int codec_opus_reserved(void)    { return (int) sizeof(enc_mem); }
#endif
