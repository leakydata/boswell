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
 * The colours are named below rather than written as three bools at each call
 * site: led_set_colour(false, true, true) says nothing about what it is for,
 * and for a while the meaning lived only in a comment that this firmware and
 * the Arduino build had drifted apart on.
 */

#ifndef BOSWELL_LED_H
#define BOSWELL_LED_H

#include <stdbool.h>
#include <stdint.h>

/* The light's whole vocabulary.
 *
 * Ordered by the question somebody actually asks looking at the device: am I
 * recording? Every bright colour means yes, and where the audio is going is
 * the second question, not the first.
 *
 *   green    recording, everything reaching the host
 *   cyan     recording, and sending a real backlog alongside it
 *   magenta  recording with no host -- into flash, to be sent later
 *   red      not recording, host connected
 *   blue     not recording, no host: advertising
 *   white    armed, and the microphone is producing nothing. The PDM driver
 *            can wedge and report every read as empty forever while every
 *            flag still says it is capturing; this is that, after trying to
 *            rebuild the stream and failing.
 *   yellow   recording with no flash available. A fault colour, and
 *            the one that is not obvious: without it a device whose QSPI
 *            never came up looks perfectly healthy while quietly unable to
 *            keep anything the radio cannot carry.
 *
 * Macros rather than an enum because led_set_colour takes three channels, and
 * C has no overloads to hide that behind. */
#define LED_RECORDING     false, true,  false   /* green   */
#define LED_CATCHING_UP   false, true,  true    /* cyan    */
#define LED_BUFFERING     true,  false, true    /* magenta */
#define LED_IDLE_LINKED   true,  false, false   /* red     */
#define LED_IDLE_WAITING  false, false, true    /* blue    */
#define LED_NO_FLASH      true,  true,  false   /* yellow  */
#define LED_NO_AUDIO      true,  true,  true    /* white   */

int  led_init(void);
/* What the LED *should* show. In pulse mode it is displayed briefly. */
void led_set_colour(bool r, bool g, bool b);
void led_set_level(uint8_t level);   /* 0 off .. 255 full */
void led_set_mode(uint8_t mode);     /* 0 steady, 1 pulse */
/* Call regularly; drives the pulse timing. */
void led_service(void);

/* Drive the three channels directly, bypassing the state machine, so what
 * the code asks for can be compared against what the light actually does.
 * Guessing at that from a description of the colour does not converge. */
void led_force(bool r, bool g, bool b);
bool led_ready(void);
/* Actual pin levels for red, green, blue. These are common anode: 0 lights
 * the channel. Lets the off-state be checked without anyone looking. */
void led_pin_levels(uint8_t out[3]);

#endif
