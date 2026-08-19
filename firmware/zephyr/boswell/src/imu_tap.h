/*
 * Double-tap detection on the LSM6DS3TR-C.
 *
 * The Arduino build bit-bangs this bus because the nRF52 core's TWIM has no
 * timeout and wedges loop() forever when the IMU does not answer. Zephyr's
 * driver takes a timeout, so this is an ordinary I2C client, and the board's
 * own devicetree already powers the part through a regulator-boot-on node
 * with the right high-drive setting -- the P1.08 detail that had to be
 * reverse-engineered for Arduino.
 */

#ifndef BOSWELL_IMU_TAP_H
#define BOSWELL_IMU_TAP_H

#include <stdbool.h>
#include <stdint.h>

/* cb runs on the system workqueue, not in the GPIO interrupt. */
int  imu_tap_init(void (*cb)(void));

bool    imu_tap_present(void);
uint8_t imu_tap_addr(void);
/* bus1@6A, bus1@6B, bus2@6A, bus2@6B -- same four probe slots the host
 * already prints, so one host works against both firmwares. */
void    imu_tap_probe(uint8_t out[4]);
void    imu_tap_set_threshold(uint8_t thresh);
/* Re-run detection at runtime. Bring-up on this part is fiddly enough that
 * being able to re-probe from the shell beats a reflash per hypothesis. */
int     imu_tap_reprobe(void);
/* irq edges, DOUBLE_TAP events, accepted, debounced */
void    imu_tap_counters(uint32_t out[4]);
void     imu_tap_set_debounce(uint32_t ms);
uint32_t imu_tap_get_debounce(void);
uint8_t  imu_tap_get_threshold(void);
/* Tap detection can be switched off without unbinding the interrupt, so a
 * wearer who keeps knocking the device can stop it toggling capture. */
void     imu_tap_set_enabled(bool on);

/* Motion, from the part's own embedded functions rather than from streaming
 * samples and counting peaks on the CPU. The step counter, tilt detector and
 * significant-motion detector all run inside the LSM6DS3TR-C and cost
 * nothing to leave on, which is what makes them usable on a device that has
 * to last a day. */
uint32_t imu_steps(void);
void     imu_steps_reset(void);
bool     imu_tilt(void);            /* seen since the last read */
bool     imu_significant_motion(void);
void     imu_motion_poll(void);     /* refresh the cached readings */
/* CTRL10_C read back from the part. Zero steps on a desk is the same reading
 * as a pedometer that was never switched on, so the configuration is made
 * observable rather than assumed. */
uint8_t  imu_motion_config(void);

/* One raw sample. Accelerometer always; gyroscope only when it has been
 * switched on, because it costs about a hundred times the current. */
struct imu_sample {
    int16_t ax, ay, az;
    int16_t gx, gy, gz;
};
bool imu_read_motion(struct imu_sample *out, bool with_gyro);
void imu_set_gyro(bool on);
bool imu_gyro_enabled(void);
bool     imu_tap_enabled(void);

#endif
