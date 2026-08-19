#include "led.h"

#include <zephyr/device.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(led, LOG_LEVEL_INF);

#define PULSE_EVERY_MS 3000
#define PULSE_LIT_MS   25

static const struct pwm_dt_spec ch_r = PWM_DT_SPEC_GET(DT_NODELABEL(pwm_r));
static const struct pwm_dt_spec ch_g = PWM_DT_SPEC_GET(DT_NODELABEL(pwm_g));
static const struct pwm_dt_spec ch_b = PWM_DT_SPEC_GET(DT_NODELABEL(pwm_b));

static bool    want_r, want_g, want_b;
static uint8_t level = 255;
static uint8_t mode  = 1;
static bool    ready;

static void drive(const struct pwm_dt_spec *ch, bool on)
{
    if (!ready) {
        return;
    }
    uint32_t duty = 0;

    if (on && level > 0) {
        duty = ((uint64_t)ch->period * level) / 255U;
    }
    (void)pwm_set_pulse_dt(ch, duty);
}

static void apply(bool r, bool g, bool b)
{
    drive(&ch_r, r);
    drive(&ch_g, g);
    drive(&ch_b, b);
}

int led_init(void)
{
    if (!pwm_is_ready_dt(&ch_r) || !pwm_is_ready_dt(&ch_g) ||
        !pwm_is_ready_dt(&ch_b)) {
        LOG_ERR("PWM channels not ready");
        return -ENODEV;
    }
    ready = true;
    apply(false, false, false);
    return 0;
}

void led_set_colour(bool r, bool g, bool b)
{
    want_r = r; want_g = g; want_b = b;
    if (mode == 1) {
        return;              /* the pulse timer owns the output */
    }
    apply(r, g, b);
}

void led_set_level(uint8_t l)
{
    level = l;
    if (mode == 0) {
        apply(want_r, want_g, want_b);
    }
}

void led_set_mode(uint8_t m)
{
    mode = m ? 1 : 0;
    if (mode == 0) {
        apply(want_r, want_g, want_b);
    } else {
        apply(false, false, false);
    }
}

void led_service(void)
{
    static int64_t last;
    static bool    lit;

    if (mode != 1) {
        return;
    }
    int64_t now = k_uptime_get();

    if (!lit && now - last >= PULSE_EVERY_MS) {
        apply(want_r, want_g, want_b);
        lit  = true;
        last = now;
    } else if (lit && now - last >= PULSE_LIT_MS) {
        apply(false, false, false);
        lit = false;
    }
}
