#include "tone.h"

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/i2s.h>
#include <zephyr/logging/log.h>

#include <string.h>

LOG_MODULE_REGISTER(boswell_tone, LOG_LEVEL_INF);

/* Both, not either. The node can be enabled by an overlay on a board whose
 * build does not include the I2S driver, and then the device object the
 * generated devicetree promises simply does not exist at link time. */
#if DT_NODE_HAS_STATUS(DT_NODELABEL(i2s0), okay) && defined(CONFIG_I2S)

#define RATE      16000
#define CHANNELS  2          /* the amplifier wants a stereo frame even mono */
#define BYTES     2

/* One block is 20 ms. Small on purpose: a note is written as several blocks
 * rather than held whole, so the RAM cost is a couple of kilobytes rather
 * than the length of the longest sound anyone might want. */
#define BLOCK_SAMPLES (RATE / 50)
#define BLOCK_BYTES   (BLOCK_SAMPLES * CHANNELS * BYTES)

K_MEM_SLAB_DEFINE_STATIC(tone_slab, BLOCK_BYTES, 4, 4);

static const struct device *i2s;
static bool ready;

/* A quarter of full scale. Loud enough to hear across a room on this
 * amplifier, quiet enough not to be startling on your chest. */
#define AMPLITUDE 8000

struct note { uint16_t hz; uint16_t ms; };

/* Rising means started, falling means stopped, and three falling means gone.
 * Deliberately different lengths as well as pitches: pitch alone is hard to
 * judge in a noisy room, and "how many beeps" survives that. */
static const struct note arm_notes[]    = { { 660, 90 }, { 990, 110 } };
static const struct note disarm_notes[] = { { 990, 90 }, { 660, 110 } };
static const struct note off_notes[]    = { { 880, 90 }, { 660, 90 },
                                            { 440, 160 } };

/* A square wave, not a sine.
 *
 * A sine needs sinf() and a table or the maths library on a part with no
 * FPU worth using for it. A square wave at these frequencies through a small
 * speaker sounds like a beep either way -- the harmonics are above what this
 * driver reproduces. The phase is carried across blocks so a note does not
 * click at every 20 ms boundary. */
static void fill(int16_t *dst, uint16_t hz, uint32_t *phase)
{
    uint32_t step = ((uint32_t)hz << 16) / RATE;

    for (int i = 0; i < BLOCK_SAMPLES; i++) {
        int16_t v = (*phase & 0x8000) ? AMPLITUDE : -AMPLITUDE;
        *phase = (*phase + step) & 0xFFFF;
        for (int c = 0; c < CHANNELS; c++) {
            *dst++ = v;
        }
    }
}

int tone_init(void)
{
    i2s = DEVICE_DT_GET(DT_NODELABEL(i2s0));
    if (!device_is_ready(i2s)) {
        LOG_WRN("no I2S; the device stays silent");
        return -ENODEV;
    }
    ready = true;
    return 0;
}

static int configure(void)
{
    struct i2s_config cfg = {
        .word_size      = 16,
        .channels       = CHANNELS,
        .format         = I2S_FMT_DATA_FORMAT_I2S,
        .options        = I2S_OPT_FRAME_CLK_MASTER | I2S_OPT_BIT_CLK_MASTER,
        .frame_clk_freq = RATE,
        .mem_slab       = &tone_slab,
        .block_size     = BLOCK_BYTES,
        .timeout        = 500,
    };
    return i2s_configure(i2s, I2S_DIR_TX, &cfg);
}

static void play(const struct note *notes, size_t count)
{
    if (!ready) {
        return;
    }
    if (configure() != 0) {
        LOG_WRN("i2s_configure failed");
        return;
    }

    bool started = false;

    for (size_t n = 0; n < count; n++) {
        uint32_t phase = 0;
        int blocks = (notes[n].ms * RATE / 1000) / BLOCK_SAMPLES;
        if (blocks < 1) {
            blocks = 1;
        }
        for (int b = 0; b < blocks; b++) {
            void *buf;
            if (k_mem_slab_alloc(&tone_slab, &buf, K_MSEC(200)) != 0) {
                goto done;
            }
            fill(buf, notes[n].hz, &phase);
            if (i2s_write(i2s, buf, BLOCK_BYTES) != 0) {
                k_mem_slab_free(&tone_slab, buf);
                goto done;
            }
            if (!started) {
                /* Started after the first block is queued, not before: an
                 * I2S transmitter with nothing to send underruns immediately
                 * and stops, which sounds like a click and no tone. */
                if (i2s_trigger(i2s, I2S_DIR_TX, I2S_TRIGGER_START) != 0) {
                    goto done;
                }
                started = true;
            }
        }
    }

done:
    if (started) {
        /* DRAIN, not STOP: stop cuts the last block mid-sample and the
         * speaker pops. Drain plays out what is queued and then idles. */
        i2s_trigger(i2s, I2S_DIR_TX, I2S_TRIGGER_DRAIN);
    }
}

void tone_play(enum tone which)
{
    switch (which) {
    case TONE_ARM:    play(arm_notes,    ARRAY_SIZE(arm_notes));    break;
    case TONE_DISARM: play(disarm_notes, ARRAY_SIZE(disarm_notes)); break;
    case TONE_OFF:    play(off_notes,    ARRAY_SIZE(off_notes));    break;
    }
}

#else  /* no I2S on this board */

int  tone_init(void) { return -ENODEV; }
void tone_play(enum tone which) { ARG_UNUSED(which); }

#endif
