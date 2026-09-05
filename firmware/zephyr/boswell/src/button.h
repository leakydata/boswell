#ifndef BOSWELL_BUTTON_H
#define BOSWELL_BUTTON_H

#include <stdbool.h>
#include <stdint.h>

/* A momentary switch to ground, read as gestures rather than as edges.
 *
 * The accelerometer tap this replaces has no way to tell a deliberate tap
 * from the device being set down -- the IMU sees the same impulse either way,
 * and every attempt to separate them has been a threshold compromise. A
 * switch has no such ambiguity, which is the whole reason for fitting one. */

enum button_gesture {
    BUTTON_SINGLE,   /* a press and release, on its own */
    BUTTON_DOUBLE,   /* two of those inside the pairing window */
    BUTTON_LONG,     /* held past the long-press threshold */
};

/* Install the handler. Safe to call on a board with no switch fitted: the
 * pin is held high by its pull-up and no gesture is ever reported.
 * Returns 0, or a negative errno if the pin could not be claimed. */
int button_init(void (*handler)(enum button_gesture));

/* Counters, for `boswell button` -- the same question the tap counters
 * answer, which is whether the hardware is being seen at all. */
void button_counters(uint32_t *presses, uint32_t *singles,
                     uint32_t *doubles, uint32_t *longs, uint32_t *bounces);

/* Is the switch held down right now? */
bool button_is_down(void);

#endif /* BOSWELL_BUTTON_H */
