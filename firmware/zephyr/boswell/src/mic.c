/*
 * PDM capture via Zephyr's DMIC API.
 *
 * Zephyr hands audio over as blocks from a memory slab rather than through a
 * callback, so there is no ISR-to-ring-buffer handoff to get wrong. The slab
 * is sized for roughly a second of audio: buffering to flash means erasing a
 * sector every so often, and a NOR erase blocks long enough that anything
 * shorter overruns and drops samples audibly. That was learned the hard way on
 * the Arduino build.
 */

#include "mic.h"

#include <zephyr/audio/dmic.h>
#include <nrfx_pdm.h>
#include <zephyr/device.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(mic, LOG_LEVEL_INF);

#define BLOCK_SAMPLES  MAX_SAMPLES              /* 20 ms at 16 kHz */
#define BLOCK_BYTES    (BLOCK_SAMPLES * sizeof(int16_t))
#define BLOCK_COUNT    50                       /* ~1 s of headroom */

K_MEM_SLAB_DEFINE_STATIC(mic_slab, BLOCK_BYTES, BLOCK_COUNT, 4);

static const struct device *dmic_dev;
static bool running;
static uint8_t last_gain = 0x28;        /* 0 dB until told otherwise */

static int mic_configure(void)
{
    struct pcm_stream_cfg stream = {
        .pcm_width = 16,
        .mem_slab  = &mic_slab,
        .pcm_rate  = PDM_RATE,
        .block_size = BLOCK_BYTES,
    };
    struct dmic_cfg cfg = {
        .io = {
            /* Comfortably inside the microphone's supported clock range. */
            .min_pdm_clk_freq = 1000000,
            .max_pdm_clk_freq = 3500000,
            .min_pdm_clk_dc   = 40,
            .max_pdm_clk_dc   = 60,
        },
        .streams = &stream,
        .channel = {
            .req_num_streams = 1,
            .req_num_chan    = 1,
        },
    };
    cfg.channel.req_chan_map_lo = dmic_build_channel_map(0, 0, PDM_CHAN_LEFT);

    int err = dmic_configure(dmic_dev, &cfg);
    if (err) {
        LOG_ERR("dmic_configure failed (%d)", err);
        return err;
    }
    LOG_INF("PDM configured at %d Hz, %d blocks of %d ms",
            PDM_RATE, BLOCK_COUNT, PROTO_FRAME_MS);
    return 0;
}

int mic_init(void)
{
    dmic_dev = DEVICE_DT_GET(DT_NODELABEL(pdm0));
    if (!device_is_ready(dmic_dev)) {
        LOG_ERR("DMIC not ready");
        return -ENODEV;
    }
    return mic_configure();
}

/* Put the microphone back together after it has stopped producing anything.
 *
 * The driver can end up in a state where every read returns "No audio data to
 * be read", forever, while the device still believes it is capturing: the
 * capture loop reads, fails, sleeps five milliseconds and reads again, and
 * nothing anywhere says a word. Found after a wearer spoke into the device for
 * several seconds and no recording appeared -- pushes, pages and erases all
 * frozen, the light still green, the driver logging that error five times a
 * second for eleven minutes.
 *
 * A stop and start alone does not clear it; that was tried first and the
 * counters did not move. The stream has to be configured again, which is why
 * the configuration is its own function now.
 */
int mic_recover(void)
{
    LOG_WRN("microphone produced nothing; reconfiguring the PDM stream");
    dmic_trigger(dmic_dev, DMIC_TRIGGER_STOP);
    running = false;
    k_sleep(K_MSEC(20));

    int err = mic_configure();
    if (err) {
        LOG_ERR("PDM reconfigure failed (%d)", err);
        return err;
    }
    mic_set_gain(last_gain);
    err = mic_start();
    if (err) {
        LOG_ERR("PDM restart failed (%d)", err);
    }
    return err;
}

/* The DMIC API has no gain control and the devicetree binding exposes none,
 * so the PDM peripheral's gain register is set directly. Same units as the
 * Arduino build: 0x00 is -20 dB, 0x28 is 0 dB, 0x50 is +20 dB, half a dB per
 * step. Must be applied after configuration, which resets it. */
void mic_set_gain(uint8_t gain)
{
    if (gain > 0x50) {
        gain = 0x50;
    }
    /* Kept because dmic_configure resets the register, and a recovery that
     * quietly dropped the microphone back to 0 dB would look like the fault
     * had half-worked. */
    last_gain = gain;
    nrf_pdm_gain_set(NRF_PDM0, gain, gain);
    LOG_INF("PDM gain %u (%d.%u dB)", gain,
            ((int)gain - 0x28) / 2, (((int)gain - 0x28) & 1) ? 5 : 0);
}

int mic_start(void)
{
    if (running) {
        return 0;
    }
    int err = dmic_trigger(dmic_dev, DMIC_TRIGGER_START);
    if (err) {
        LOG_ERR("dmic start failed (%d)", err);
        return err;
    }
    running = true;
    return 0;
}

void mic_stop(void)
{
    if (!running) {
        return;
    }
    dmic_trigger(dmic_dev, DMIC_TRIGGER_STOP);
    running = false;
}

bool mic_running(void)
{
    return running;
}

int mic_read_frame(int16_t *dst, int max_samples, k_timeout_t timeout)
{
    void *buf;
    uint32_t size;

    if (!running) {
        return 0;
    }
    int err = dmic_read(dmic_dev, 0, &buf, &size, k_ticks_to_ms_floor32(timeout.ticks));
    if (err) {
        return 0;
    }
    int samples = (int)(size / sizeof(int16_t));
    if (samples > max_samples) {
        /* Would silently discard the tail of the block, which is lost audio.
         * Loud rather than quiet: the frame size and block size must agree. */
        LOG_WRN("block of %d samples exceeds frame of %d", samples, max_samples);
        samples = max_samples;
    }
    memcpy(dst, buf, samples * sizeof(int16_t));
    k_mem_slab_free(&mic_slab, buf);
    return samples;
}
