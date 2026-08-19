/*
 * Persisted settings.
 *
 * The struct lives in a header because the Arduino build auto-generates
 * function prototypes and inserts them above the sketch body -- a type
 * declared in the .ino is not visible to a prototype that mentions it.
 */

#ifndef BOSWELL_SETTINGS_H
#define BOSWELL_SETTINGS_H

#include <stdint.h>

#define SETTINGS_PATH  "/boswell.cfg"
#define SETTINGS_MAGIC 0xB05E
#define SETTINGS_VER   1

struct Settings {
  uint16_t magic;
  uint8_t  version;
  uint8_t  gain;
  uint8_t  use16k;
  uint8_t  vadEnabled;
  uint16_t vadThresh;
  uint8_t  ledLevel;
  uint8_t  ledMode;
  uint8_t  backlogMode;
  uint8_t  micPowerSave;
  uint8_t  tapEnabled;
  uint8_t  fastCharge;
  int8_t   txPower;
};

#endif
