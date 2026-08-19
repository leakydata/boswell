/*
 * Boswell — Zephyr firmware.
 *
 * Same job as the Arduino build and the same wire protocol, restructured
 * around threads: capture runs on its own thread and blocks on the DMIC
 * driver, so there is no polling loop deciding how often to look at anything.
 * That polling was the source of two separate faults in the Arduino version.
 */

#include "proto.h"
#include "codec.h"
#include "mic.h"
#include "ble_audio.h"

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

struct boswell_state g_state = {
    .streaming    = false,
    .use16k       = false,       /* 8 kHz fits a Bluetooth 4.0 host */
    .vad_enabled  = false,
    .vad_thresh   = 1120,
    .gain         = 50,
    .led_level    = 255,
    .led_mode     = 1,           /* blink: ~1% of the power of staying lit */
    .backlog_mode = 0,
    .mic_power_save = 1,
    .tx_power     = 4,
};

/* ---------------------------------------------------------------- leds */

static const struct gpio_dt_spec led_r = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
static const struct gpio_dt_spec led_g = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);
static const struct gpio_dt_spec led_b = GPIO_DT_SPEC_GET(DT_ALIAS(led2), gpios);

static void led_set(bool r, bool g, bool b)
{
    gpio_pin_set_dt(&led_r, r);
    gpio_pin_set_dt(&led_g, g);
    gpio_pin_set_dt(&led_b, b);
}

/* blue advertising · green capturing · red connected but idle · magenta buffering */
static void led_state(void)
{
    if (!ble_audio_connected()) {
        led_set(g_state.streaming, false, true);
    } else if (!g_state.streaming) {
        led_set(true, false, false);
    } else {
        led_set(false, true, false);
    }
}

static void led_init(void)
{
    gpio_pin_configure_dt(&led_r, GPIO_OUTPUT_INACTIVE);
    gpio_pin_configure_dt(&led_g, GPIO_OUTPUT_INACTIVE);
    gpio_pin_configure_dt(&led_b, GPIO_OUTPUT_INACTIVE);
}

/* ---------------------------------------------------------------- watchdog */

static const struct device *wdt_dev;
static int wdt_ch = -1;

static void watchdog_init(void)
{
    wdt_dev = DEVICE_DT_GET(DT_NODELABEL(wdt0));
    if (!device_is_ready(wdt_dev)) {
        LOG_WRN("watchdog unavailable");
        return;
    }
    struct wdt_timeout_cfg cfg = {
        .window = { .min = 0, .max = 30000 },   /* generous: a flash erase must not trip it */
        .callback = NULL,
        .flags = WDT_FLAG_RESET_SOC,
    };
    wdt_ch = wdt_install_timeout(wdt_dev, &cfg);
    if (wdt_ch < 0 || wdt_setup(wdt_dev, WDT_OPT_PAUSE_HALTED_BY_DBG)) {
        LOG_WRN("watchdog setup failed");
        wdt_ch = -1;
    }
}

static inline void watchdog_feed(void)
{
    if (wdt_ch >= 0) {
        wdt_feed(wdt_dev, wdt_ch);
    }
}

/* ---------------------------------------------------------------- control */

static void on_ctrl(uint8_t op, uint8_t arg)
{
    switch (op) {
    case CTRL_STREAM:       g_state.streaming = arg != 0; break;
    case CTRL_RATE:         g_state.use16k = arg != 0;    break;
    case CTRL_GAIN:         g_state.gain = arg;           break;
    case CTRL_VAD:          g_state.vad_enabled = arg != 0; break;
    case CTRL_VAD_THRESH:   g_state.vad_thresh = arg * 32; break;
    case CTRL_BACKLOG_MODE: g_state.backlog_mode = arg ? 1 : 0; break;
    case CTRL_LED_LEVEL:    g_state.led_level = arg;      break;
    case CTRL_LED_MODE:     g_state.led_mode = arg ? 1 : 0; break;
    case CTRL_MIC_SAVE:     g_state.mic_power_save = arg ? 1 : 0; break;
    default: break;
    }
    ble_audio_publish_info();
    led_state();
}

/* ---------------------------------------------------------------- capture */

#define CAPTURE_STACK 4096
K_THREAD_STACK_DEFINE(capture_stack, CAPTURE_STACK);
static struct k_thread capture_thread;

static int16_t raw[MAX_SAMPLES];
static int16_t frame[MAX_SAMPLES];
static uint8_t wire[MAX_FRAME_LEN];

static void capture_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    uint16_t seq = 0;
    int hangover = 0;

    while (1) {
        watchdog_feed();

        if (!g_state.streaming) {
            if (g_state.mic_power_save && mic_running()) {
                mic_stop();       /* the microphone is ~1 mA doing nothing */
            }
            k_sleep(K_MSEC(50));
            continue;
        }
        if (!mic_running() && mic_start() != 0) {
            k_sleep(K_MSEC(200));
            continue;
        }

        int got = mic_read_frame(raw, MAX_SAMPLES, K_MSEC(200));
        if (got <= 0) {
            continue;
        }

        int count = got;
        if (!g_state.use16k) {
            /* 2:1 decimation by pair averaging: a 2-tap filter whose null sits
             * at 4 kHz, which is where the fold lands. */
            count = got / 2;
            for (int i = 0; i < count; i++) {
                frame[i] = (int16_t)(((int32_t)raw[2 * i] + raw[2 * i + 1]) / 2);
            }
        } else {
            memcpy(frame, raw, got * sizeof(int16_t));
        }

        bool voiced = true;
        if (g_state.vad_enabled) {
            uint32_t rms = codec_rms(frame, count);
            if (rms >= g_state.vad_thresh) {
                hangover = 15;              /* 300 ms release */
            } else if (hangover > 0) {
                hangover--;
            }
            voiced = hangover > 0;
            if (!voiced) {
                seq++;                      /* the gap is intentional, not loss */
                continue;
            }
        }

        uint8_t flags = 0;
        if (g_state.use16k)      flags |= FLAG_16K;
        if (voiced)              flags |= FLAG_VOICED;
        if (g_state.vad_enabled) flags |= FLAG_VAD_ON;

        uint16_t len = codec_build_frame(frame, count, seq,
                                         k_uptime_get_32(), flags, wire);
        if (ble_audio_send(wire, len) == 0) {
            seq++;
        }
    }
}

/* ---------------------------------------------------------------- main */

int main(void)
{
    /* USB and the LED come first so that whatever happens next can be seen.
     * The first version of this initialised the microphone and Bluetooth
     * before either, and when it faulted there was no way to tell why. */
    led_init();
    led_set(false, false, true);
    (void)usb_enable(NULL);
    k_sleep(K_MSEC(1500));          /* let a host enumerate before we talk */
    LOG_INF("Boswell starting");

    int err = mic_init();
    LOG_INF("mic_init -> %d", err);

    err = ble_audio_init(on_ctrl);
    LOG_INF("ble_audio_init -> %d", err);

    watchdog_init();
    LOG_INF("watchdog ready");

    k_thread_create(&capture_thread, capture_stack, CAPTURE_STACK,
                    capture_fn, NULL, NULL, NULL,
                    K_PRIO_COOP(7), 0, K_NO_WAIT);
    k_thread_name_set(&capture_thread, "capture");

    while (1) {
        watchdog_feed();
        led_state();
        k_sleep(K_MSEC(500));
    }
    return 0;
}
