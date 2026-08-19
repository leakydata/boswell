#include "battery.h"

#include <zephyr/device.h>
#include <zephyr/drivers/adc.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(battery, LOG_LEVEL_INF);

/* 1M over 510k. */
#define VBAT_NUM   1510
#define VBAT_DEN   510

static const struct adc_dt_spec vbat_adc =
    ADC_DT_SPEC_GET_BY_IDX(DT_PATH(zephyr_user), 0);

/* Not in the board devicetree as named nodes, so addressed by number. */
static const struct gpio_dt_spec vbat_enable = {
    .port = DEVICE_DT_GET(DT_NODELABEL(gpio0)), .pin = 14, .dt_flags = 0,
};
static const struct gpio_dt_spec chg_pin = {
    .port = DEVICE_DT_GET(DT_NODELABEL(gpio0)), .pin = 17, .dt_flags = 0,
};

static bool     ready;
static uint16_t cached_mv;

int battery_init(void)
{
    if (!adc_is_ready_dt(&vbat_adc)) {
        LOG_ERR("ADC not ready");
        return -ENODEV;
    }
    int err = adc_channel_setup_dt(&vbat_adc);
    if (err) {
        LOG_ERR("adc_channel_setup failed (%d)", err);
        return err;
    }
    /* Start disconnected: the divider draws ~8 uA continuously while it is
     * connected, which matters on a device meant to run all day. */
    gpio_pin_configure_dt(&vbat_enable, GPIO_OUTPUT_HIGH);
    gpio_pin_configure_dt(&chg_pin, GPIO_INPUT | GPIO_PULL_UP);

    ready = true;
    battery_sample();
    LOG_INF("battery %u mV (%u%%)%s", cached_mv, battery_percent(),
            battery_charging() ? ", charging" : "");
    return 0;
}

void battery_sample(void)
{
    if (!ready) {
        return;
    }
    int16_t buf = 0;
    struct adc_sequence seq = { .buffer = &buf, .buffer_size = sizeof(buf) };
    (void)adc_sequence_init_dt(&vbat_adc, &seq);

    gpio_pin_set_dt(&vbat_enable, 0);      /* connect the divider */
    k_busy_wait(200);

    int32_t acc = 0, n = 0;
    for (int i = 0; i < 8; i++) {
        if (adc_read_dt(&vbat_adc, &seq) == 0) {
            acc += buf;
            n++;
        }
    }
    gpio_pin_set_dt(&vbat_enable, 1);      /* and disconnect it again */

    if (n == 0) {
        cached_mv = 0;
        return;
    }
    int32_t mv = acc / n;
    if (adc_raw_to_millivolts_dt(&vbat_adc, &mv) != 0) {
        cached_mv = 0;
        return;
    }
    cached_mv = (uint16_t)((mv * VBAT_NUM) / VBAT_DEN);
}

uint16_t battery_mv(void) { return cached_mv; }

bool battery_charging(void)
{
    if (!ready) {
        return false;
    }
    return gpio_pin_get_dt(&chg_pin) == 0;   /* ~CHG is active low */
}

/* A single-cell lithium curve is flat through the middle, so a linear map from
 * volts to percent is misleading. These breakpoints track the discharge curve
 * closely enough to be useful without pretending to precision. Same table as
 * the Arduino build, so the two firmwares report the same number. */
uint8_t battery_percent(void)
{
    static const uint16_t pts[][2] = {
        {4150, 100}, {4050, 90}, {3950, 75}, {3850, 60}, {3800, 50},
        {3750, 40}, {3700, 30}, {3650, 20}, {3550, 10}, {3400, 5}, {3200, 0}
    };
    uint16_t mv = cached_mv;

    if (mv == 0) {
        return 0;
    }
    if (mv >= pts[0][0]) {
        return 100;
    }
    for (size_t i = 1; i < ARRAY_SIZE(pts); i++) {
        if (mv >= pts[i][0]) {
            uint16_t hi_v = pts[i - 1][0], lo_v = pts[i][0];
            uint8_t  hi_p = pts[i - 1][1], lo_p = pts[i][1];
            return (uint8_t)(lo_p + (int32_t)(mv - lo_v) * (hi_p - lo_p) / (hi_v - lo_v));
        }
    }
    return 0;
}
