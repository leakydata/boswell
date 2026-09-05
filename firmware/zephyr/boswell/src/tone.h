#ifndef BOSWELL_TONE_H
#define BOSWELL_TONE_H

/*
 * Short sounds, so the device can say what it just did.
 *
 * The status light is the only feedback a wearer has had, and it is on the
 * device -- which is on your chest. You cannot see it without taking the
 * thing off, which is exactly when you most want to know whether the press
 * you just made started or stopped a recording. A rising pair and a falling
 * pair are distinguishable without looking, and without counting.
 *
 * Power-off gets its own, and it matters most of the three: a device that
 * goes quiet with no sound is indistinguishable from one that has crashed,
 * and this project has had enough of those to know the difference is worth
 * two hundred milliseconds of amplifier.
 */

enum tone {
    TONE_ARM,      /* rising pair -- recording */
    TONE_DISARM,   /* falling pair -- stopped */
    TONE_OFF,      /* three descending -- powering down */
};

/* Returns 0, or a negative errno if there is no I2S on this board. */
int tone_init(void);

/* Plays and returns once the sound has finished. Called from places that can
 * afford a couple of hundred milliseconds -- a button gesture, not the audio
 * path. Silently does nothing if init failed. */
void tone_play(enum tone which);

#endif /* BOSWELL_TONE_H */
