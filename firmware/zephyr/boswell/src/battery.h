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

#endif
