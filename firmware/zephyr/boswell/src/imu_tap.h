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

#endif
