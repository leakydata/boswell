/*
 * Battery sense for the XIAO nRF52840.
 *
 * VBAT reaches AIN7 through a 1M/510k divider that is disconnected until
 * VBAT_ENABLE (P0.14) is driven low, so a reading taken without enabling it
 * is meaningless rather than merely wrong. The BQ25101's ~CHG output on P0.17
 * is active low.
 */

#ifndef BOSWELL_BATTERY_H
#define BOSWELL_BATTERY_H

#include <stdbool.h>
#include <stdint.h>

int      battery_init(void);
uint16_t battery_mv(void);        /* 0 if unavailable */
uint8_t  battery_percent(void);
bool     battery_charging(void);
void     battery_sample(void);    /* refresh the cached reading */
/* BQ25100 charge current. The pin is driven low for 100 mA and left floating
 * for the 50 mA default, so this is a tri-state, not a level. */
void     battery_set_fast_charge(bool on);
bool     battery_fast_charge(void);

/* Pin operations the driver refused. */
uint32_t battery_gpio_fails(void);

#endif
