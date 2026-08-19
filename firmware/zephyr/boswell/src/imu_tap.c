#include "imu_tap.h"

#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(imu, LOG_LEVEL_INF);

#define IMU_ADDR_A      0x6A
#define IMU_ADDR_B      0x6B

#define REG_WHO_AM_I    0x0F
#define REG_TAP_SRC     0x1C
#define REG_CTRL1_XL    0x10
#define REG_TAP_CFG     0x58
#define REG_TAP_THS_6D  0x59
#define REG_INT_DUR2    0x5A
#define REG_WAKE_UP_THS 0x5B
#define REG_MD1_CFG     0x5E

#define TAP_SRC_DOUBLE  0x10
#define TAP_SRC_SINGLE  0x20
#define TAP_SRC_IA      0x40

static const struct device *i2c_dev;
static const struct gpio_dt_spec irq_pin =
    GPIO_DT_SPEC_GET(DT_NODELABEL(lsm6ds3tr_c), irq_gpios);

static struct gpio_callback irq_cb_data;
static struct k_work        tap_work;
static void (*user_cb)(void);

/* Default only; tunable at runtime via the shell. */
#define TAP_DEBOUNCE_DEFAULT_MS 2500

static uint8_t imu_addr;                 /* 0 until a probe answers */
static int64_t last_tap_ms;

/* Counters, so a missed tap can be told apart from a debounced one. Without
 * this the only observable is "nothing happened", which fits both a chip that
 * never saw the tap and a debounce window that swallowed it -- and those want
 * opposite fixes. */
static uint32_t n_irq;        /* INT1 edges */
static uint32_t n_double;     /* DOUBLE_TAP set in TAP_SRC */
static uint32_t n_accepted;   /* survived debounce */
static uint32_t n_debounced;
static uint32_t debounce_ms = TAP_DEBOUNCE_DEFAULT_MS;

/* Two consecutive toggles closer together than this are treated as one event.
 * One physical tap rings the accelerometer into many events: a measured run
 * saw 77 DOUBLE_TAP interrupts, of which 32 still landed more than 600 ms
 * apart, so capture toggled dozens of times for a handful of taps. Detection
 * was never the weak point -- suppression was. A person toggling recording
 * does not need sub-second response, so the window is generous. */
static uint8_t probe_results[4] = { 0xFF, 0xFF, 0xFF, 0xFF };

static int reg_write(uint8_t reg, uint8_t val)
{
    return i2c_reg_write_byte(i2c_dev, imu_addr, reg, val);
}

static int reg_read(uint8_t addr, uint8_t reg, uint8_t *val)
{
    return i2c_reg_read_byte(i2c_dev, addr, reg, val);
}

/* Runs on the system workqueue. Reading TAP_SRC also clears the latched
 * interrupt, so this must happen outside the GPIO callback. */
static void tap_work_fn(struct k_work *work)
{
    ARG_UNUSED(work);
    uint8_t src = 0;

    if (reg_read(imu_addr, REG_TAP_SRC, &src) != 0) {
        return;
    }
    if (src & TAP_SRC_DOUBLE) {
        n_double++;
    }
    if (src & TAP_SRC_DOUBLE) {
        int64_t now = k_uptime_get();
        if (now - last_tap_ms < (int64_t)debounce_ms) {
            n_debounced++;
            last_tap_ms = now;   /* a burst extends the window */
            return;
        }
        last_tap_ms = now;
        n_accepted++;
        LOG_INF("double tap (src 0x%02x)", src);
        if (user_cb) {
            user_cb();
        }
    }
}

static void irq_handler(const struct device *port, struct gpio_callback *cb,
                        gpio_port_pins_t pins)
{
    ARG_UNUSED(port); ARG_UNUSED(cb); ARG_UNUSED(pins);
    n_irq++;
    k_work_submit(&tap_work);
}

int imu_tap_init(void (*cb)(void))
{
    user_cb = cb;

    i2c_dev = DEVICE_DT_GET(DT_NODELABEL(i2c0));
    if (!device_is_ready(i2c_dev)) {
        LOG_ERR("i2c0 not ready");
        return -ENODEV;
    }

    /* The regulator powering the part has a 3 ms startup delay in the board
     * devicetree; it is enabled at boot, but give the part time to answer. */
    k_sleep(K_MSEC(20));

    /* Probe both addresses. The host prints four slots because the Arduino
     * build has two candidate buses; Zephyr routes the sensors to i2c0 only,
     * so slots 0 and 1 are the real ones and 2 and 3 stay 0xFF. */
    const uint8_t candidates[2] = { IMU_ADDR_A, IMU_ADDR_B };
    for (int i = 0; i < 2; i++) {
        uint8_t who = 0xFF;
        if (reg_read(candidates[i], REG_WHO_AM_I, &who) == 0) {
            probe_results[i] = who;
            /* LSM6DS3 reports 0x69, LSM6DS3TR-C reports 0x6A. */
            if ((who == 0x6A || who == 0x69) && imu_addr == 0) {
                imu_addr = candidates[i];
            }
        }
    }
    if (imu_addr == 0) {
        LOG_WRN("no IMU (probe %02x %02x)", probe_results[0], probe_results[1]);
        return -ENODEV;
    }
    LOG_INF("IMU at 0x%02x (WHO_AM_I 0x%02x)", imu_addr,
            probe_results[imu_addr == IMU_ADDR_A ? 0 : 1]);

    /* Same register programme as the Arduino build, which was tuned against
     * real taps on real hardware. Threshold is tunable from the shell
     * ("boswell tap <n>"): lower is more sensitive, and the useful range is
     * narrow enough that guessing at compile time wastes a flash cycle. */
    reg_write(REG_CTRL1_XL,    0x60);   /* 416 Hz, +/-2 g; tap needs a high ODR */
    reg_write(REG_TAP_CFG,     0x8F);   /* interrupts on, tap X/Y/Z, latched */
    reg_write(REG_TAP_THS_6D,  0x84);   /* D4D_EN | threshold 4 */
    reg_write(REG_INT_DUR2,    0x7F);   /* gap/quiet/shock windows reject bumps */
    reg_write(REG_WAKE_UP_THS, 0x80);   /* SINGLE_DOUBLE_TAP: double-tap mode */
    reg_write(REG_MD1_CFG,     0x08);   /* route double-tap to INT1 */

    k_work_init(&tap_work, tap_work_fn);

    if (!gpio_is_ready_dt(&irq_pin)) {
        LOG_ERR("IMU interrupt pin not ready");
        return -ENODEV;
    }
    gpio_pin_configure_dt(&irq_pin, GPIO_INPUT);
    gpio_pin_interrupt_configure_dt(&irq_pin, GPIO_INT_EDGE_TO_ACTIVE);
    gpio_init_callback(&irq_cb_data, irq_handler, BIT(irq_pin.pin));
    gpio_add_callback(irq_pin.port, &irq_cb_data);

    /* Clear anything latched from power-on so the first real tap is the
     * first event delivered. */
    uint8_t junk;
    reg_read(imu_addr, REG_TAP_SRC, &junk);

    LOG_INF("double-tap ready on INT1");
    return 0;
}

int imu_tap_reprobe(void)
{
    imu_addr = 0;
    for (int i = 0; i < 4; i++) {
        probe_results[i] = 0xFF;
    }
    return imu_tap_init(user_cb);
}

void imu_tap_set_debounce(uint32_t ms) { debounce_ms = ms; }
uint32_t imu_tap_get_debounce(void)     { return debounce_ms; }

void imu_tap_counters(uint32_t out[4])
{
    out[0] = n_irq; out[1] = n_double; out[2] = n_accepted; out[3] = n_debounced;
}

bool imu_tap_present(void)   { return imu_addr != 0; }
uint8_t imu_tap_addr(void)   { return imu_addr; }

void imu_tap_probe(uint8_t out[4])
{
    for (int i = 0; i < 4; i++) {
        out[i] = probe_results[i];
    }
}

void imu_tap_set_threshold(uint8_t thresh)
{
    if (imu_addr) {
        reg_write(REG_TAP_THS_6D, 0x80 | (thresh & 0x1F));
    }
}
