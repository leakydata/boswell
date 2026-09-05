#include "button.h"

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(boswell_button, LOG_LEVEL_INF);

#if DT_NODE_EXISTS(DT_ALIAS(sw0))

static const struct gpio_dt_spec sw = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

/* Timings are Omi's, which were measured against a real switch on this same
 * part rather than reasoned about. Copying them costs nothing and skips the
 * round of "is 500 ms too long" that inventing them would need.
 *
 * DEBOUNCE_MS is this project's own. A tactile switch bounces for a few
 * milliseconds on both edges; 30 ms is well past that and far short of the
 * fastest deliberate press. */
#define DEBOUNCE_MS   30
#define LONG_MS     3000    /* held this long is a long press */
#define PAIR_MS      600    /* a second press inside this pairs into a double */

static void (*on_gesture)(enum button_gesture);

static struct gpio_callback cb;
static struct k_work_delayable settle_work;   /* debounce */
static struct k_work_delayable pair_work;     /* "no second press came" */
static struct k_work_delayable long_work;     /* "still held" */

static atomic_t presses, singles, doubles, longs, bounces;

static bool     down;            /* debounced state */
static bool     long_fired;      /* this hold already reported BUTTON_LONG */
static bool     awaiting_pair;   /* one press seen, waiting out PAIR_MS */
static int64_t  down_at;

static void report(enum button_gesture g)
{
    switch (g) {
    case BUTTON_SINGLE: atomic_inc(&singles); break;
    case BUTTON_DOUBLE: atomic_inc(&doubles); break;
    case BUTTON_LONG:   atomic_inc(&longs);   break;
    }
    if (on_gesture) {
        on_gesture(g);
    }
}

/* PAIR_MS passed with no second press, so the one that is waiting was single
 * after all. Reported here rather than on release, which is what makes a
 * double tap possible at all: a single cannot be announced until it is known
 * not to be the first half of something else. */
static void pair_expired(struct k_work *w)
{
    ARG_UNUSED(w);
    if (awaiting_pair) {
        awaiting_pair = false;
        report(BUTTON_SINGLE);
    }
}

/* The switch has been held for LONG_MS. Reported while it is still down,
 * not on release, so the gesture lands when the user has held it long
 * enough rather than when they happen to let go. */
static void long_held(struct k_work *w)
{
    ARG_UNUSED(w);
    if (down && !long_fired) {
        long_fired = true;
        /* A long press is its own thing, so anything half-collected is not
         * also a tap. */
        awaiting_pair = false;
        k_work_cancel_delayable(&pair_work);
        report(BUTTON_LONG);
    }
}

static void settled(struct k_work *w)
{
    ARG_UNUSED(w);

    bool now = gpio_pin_get_dt(&sw) > 0;
    if (now == down) {
        /* The line went and came back inside the debounce window, so there
         * was no real transition -- only contact bounce. */
        atomic_inc(&bounces);
        return;
    }
    down = now;

    if (down) {
        atomic_inc(&presses);
        down_at    = k_uptime_get();
        long_fired = false;
        k_work_reschedule(&long_work, K_MSEC(LONG_MS));
        return;
    }

    k_work_cancel_delayable(&long_work);

    if (long_fired) {
        /* Already reported while held; the release is not a second gesture. */
        return;
    }

    int64_t held = k_uptime_get() - down_at;
    if (held >= LONG_MS) {
        /* Belt and braces: if the work item was delayed past the release,
         * the hold still counts for what it was. */
        report(BUTTON_LONG);
        return;
    }

    if (awaiting_pair) {
        awaiting_pair = false;
        k_work_cancel_delayable(&pair_work);
        report(BUTTON_DOUBLE);
    } else {
        awaiting_pair = true;
        k_work_reschedule(&pair_work, K_MSEC(PAIR_MS));
    }
}

static void edge(const struct device *port, struct gpio_callback *c,
                 gpio_port_pins_t pins)
{
    ARG_UNUSED(port); ARG_UNUSED(c); ARG_UNUSED(pins);
    /* Nothing is decided in interrupt context. The pin is read once it has
     * stopped moving, which is also what makes a bounce countable rather
     * than indistinguishable from a very fast press. */
    k_work_reschedule(&settle_work, K_MSEC(DEBOUNCE_MS));
}

int button_init(void (*handler)(enum button_gesture))
{
    on_gesture = handler;

    if (!gpio_is_ready_dt(&sw)) {
        LOG_ERR("button pin not ready");
        return -ENODEV;
    }

    int err = gpio_pin_configure_dt(&sw, GPIO_INPUT);
    if (err) {
        LOG_ERR("button pin configure: %d", err);
        return err;
    }

    k_work_init_delayable(&settle_work, settled);
    k_work_init_delayable(&pair_work,   pair_expired);
    k_work_init_delayable(&long_work,   long_held);

    err = gpio_pin_interrupt_configure_dt(&sw, GPIO_INT_EDGE_BOTH);
    if (err) {
        LOG_ERR("button interrupt configure: %d", err);
        return err;
    }

    gpio_init_callback(&cb, edge, BIT(sw.pin));
    gpio_add_callback(sw.port, &cb);

    down = gpio_pin_get_dt(&sw) > 0;
    if (down) {
        /* Held at boot means the switch is stuck, miswired, or the pin was
         * chosen badly -- all of which look identical to a device that
         * powers up already toggling itself. Say so once. */
        LOG_WRN("button reads pressed at boot; check the wiring");
    }

    LOG_INF("button on %s pin %d", sw.port->name, sw.pin);
    return 0;
}

void button_counters(uint32_t *p, uint32_t *s, uint32_t *d, uint32_t *l,
                     uint32_t *b)
{
    if (p) *p = (uint32_t) atomic_get(&presses);
    if (s) *s = (uint32_t) atomic_get(&singles);
    if (d) *d = (uint32_t) atomic_get(&doubles);
    if (l) *l = (uint32_t) atomic_get(&longs);
    if (b) *b = (uint32_t) atomic_get(&bounces);
}

bool button_is_down(void)
{
    return down;
}

#else  /* no sw0 in the devicetree */

int button_init(void (*handler)(enum button_gesture))
{
    ARG_UNUSED(handler);
    return -ENODEV;
}

void button_counters(uint32_t *p, uint32_t *s, uint32_t *d, uint32_t *l,
                     uint32_t *b)
{
    if (p) *p = 0;
    if (s) *s = 0;
    if (d) *d = 0;
    if (l) *l = 0;
    if (b) *b = 0;
}

bool button_is_down(void) { return false; }

#endif
