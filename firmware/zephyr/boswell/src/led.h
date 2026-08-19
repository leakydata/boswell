/*
 * RGB status LED.
 *
 * Two modes, because a light on a device worn all day is a real power cost:
 *
 *   mode 1 (default)  a brief flash every few seconds -- under 1% duty, which
 *                     beats any practical dimming level and still says the
 *                     device is alive and what it is doing.
 *   mode 0            steady, at `level` brightness.
 *
 * Colour meaning: blue advertising, green capturing, red connected but idle,
 * magenta draining the flash backlog.
 */

#ifndef BOSWELL_LED_H
#define BOSWELL_LED_H

#include <stdbool.h>
#include <stdint.h>

int  led_init(void);
/* What the LED *should* show. In pulse mode it is displayed briefly. */
void led_set_colour(bool r, bool g, bool b);
void led_set_level(uint8_t level);   /* 0 off .. 255 full */
void led_set_mode(uint8_t mode);     /* 0 steady, 1 pulse */
/* Call regularly; drives the pulse timing. */
void led_service(void);

#endif
