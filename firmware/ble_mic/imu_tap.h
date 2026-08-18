/*
 * Double-tap detection on the LSM6DS3TR-C (XIAO nRF52840 Sense).
 *
 * The sensor detects taps in hardware and raises INT1; the MCU only reads a
 * status byte once the pin fires. No sampling loop and no DSP, so this costs
 * almost nothing in CPU or flash.
 *
 * I2C is bit-banged rather than using Wire1. The nRF52 core's TWIM driver
 * spins on hardware events with no timeout, so a single unresponsive device
 * wedges the main loop forever -- which is exactly what happened here: BLE
 * kept advertising (SoftDevice is interrupt-driven) while audio silently
 * stopped. Every loop below is bounded, so the worst case is a failed read.
 */

#ifndef IMU_TAP_H
#define IMU_TAP_H

#include <Arduino.h>

/* Which bus the IMU actually sits on is not obvious: the variant defines a
 * dedicated Wire1 (17/16) next to the IMU power and INT pins, but Seeed's own
 * demo code uses plain Wire (4/5). Probe both rather than trust either. */
static uint8_t imuSda = PIN_WIRE1_SDA;   // 17
static uint8_t imuScl = PIN_WIRE1_SCL;   // 16

#define IMU_ADDR_A          0x6A
#define IMU_ADDR_B          0x6B
#define IMU_WHOAMI_VAL      0x6A                 // LSM6DS3TR-C

#define REG_WHO_AM_I        0x0F
#define REG_TAP_SRC         0x1C
#define REG_CTRL1_XL        0x10
#define REG_TAP_CFG         0x58
#define REG_TAP_THS_6D      0x59
#define REG_INT_DUR2        0x5A
#define REG_WAKE_UP_THS     0x5B
#define REG_MD1_CFG         0x5E

#define I2C_DELAY_US        5                    // ~100 kHz
#define I2C_STRETCH_LIMIT   1000                 // bounded clock stretching

static uint8_t imuAddr = 0;
static uint8_t imuBus  = 0;   // 0 none, 1 = Wire1 17/16, 2 = Wire 4/5
/* Raw WHO_AM_I from every probe, reported over GATT for diagnosis.
 * 0xFF => bus idle/no pull-ups reaching us; 0x00 => stuck low; 0x69/0x6A => IMU. */
static uint8_t imuProbe[4] = {0, 0, 0, 0};
static uint8_t imuPwrUsed = 0xFF;

/* Diagnostics. TAP_SRC bit6 TAP_IA (any tap), bit5 SINGLE_TAP, bit4 DOUBLE_TAP.
 * Counting all three separates "no taps seen at all" from "seen as single, not
 * double" -- different bugs with different fixes. */
static uint8_t  tapIaCount = 0, tapSingleCount = 0, tapDoubleCount = 0;
static uint8_t  tapLastSrc = 0;
static uint8_t  tapIntHighCount = 0;
static inline void bump(uint8_t *c) { if (*c < 255) (*c)++; }

/* Peak |Z - 1g| seen since the last report, in units of 256 LSB (~15.6 mg).
 * This answers the question the tap counters cannot: is any mechanical shock
 * actually reaching the sensor? If this stays flat while tapping, the problem
 * is the tap never arrives -- not the tap configuration. */
static uint16_t accelPeak = 0;


/* Open-drain emulation: drive low, or release and let the pull-up win. */
static inline void sdaHigh() { pinMode(imuSda, INPUT_PULLUP); }
static inline void sdaLow()  { pinMode(imuSda, OUTPUT); digitalWrite(imuSda, LOW); }
static inline void sclLow()  { pinMode(imuScl, OUTPUT); digitalWrite(imuScl, LOW); }

/* Release SCL and wait out any clock stretching, but never indefinitely. */
static inline bool sclHigh() {
  pinMode(imuScl, INPUT_PULLUP);
  for (int i = 0; i < I2C_STRETCH_LIMIT; i++) {
    if (digitalRead(imuScl)) return true;
    delayMicroseconds(1);
  }
  return false;
}

static bool i2cStart() {
  sdaHigh(); if (!sclHigh()) return false;
  delayMicroseconds(I2C_DELAY_US);
  sdaLow();  delayMicroseconds(I2C_DELAY_US);
  sclLow();  delayMicroseconds(I2C_DELAY_US);
  return true;
}

static void i2cStop() {
  sdaLow();  delayMicroseconds(I2C_DELAY_US);
  sclHigh(); delayMicroseconds(I2C_DELAY_US);
  sdaHigh(); delayMicroseconds(I2C_DELAY_US);
}

/* Returns true on ACK. */
static bool i2cWriteByte(uint8_t b) {
  for (int i = 0; i < 8; i++) {
    if (b & 0x80) sdaHigh(); else sdaLow();
    b <<= 1;
    delayMicroseconds(I2C_DELAY_US);
    if (!sclHigh()) return false;
    delayMicroseconds(I2C_DELAY_US);
    sclLow();
    delayMicroseconds(I2C_DELAY_US);
  }
  sdaHigh();                                   // release for the ACK bit
  delayMicroseconds(I2C_DELAY_US);
  if (!sclHigh()) return false;
  bool ack = digitalRead(imuSda) == 0;
  delayMicroseconds(I2C_DELAY_US);
  sclLow();
  delayMicroseconds(I2C_DELAY_US);
  return ack;
}

static bool i2cReadByte(uint8_t *out, bool ack) {
  uint8_t v = 0;
  sdaHigh();
  for (int i = 0; i < 8; i++) {
    delayMicroseconds(I2C_DELAY_US);
    if (!sclHigh()) return false;
    v = (uint8_t)((v << 1) | (digitalRead(imuSda) ? 1 : 0));
    delayMicroseconds(I2C_DELAY_US);
    sclLow();
  }
  if (ack) sdaLow(); else sdaHigh();
  delayMicroseconds(I2C_DELAY_US);
  if (!sclHigh()) return false;
  delayMicroseconds(I2C_DELAY_US);
  sclLow();
  sdaHigh();
  *out = v;
  return true;
}

static bool imuWrite(uint8_t reg, uint8_t val) {
  if (!i2cStart()) return false;
  bool ok = i2cWriteByte((uint8_t)(imuAddr << 1)) &&
            i2cWriteByte(reg) && i2cWriteByte(val);
  i2cStop();
  return ok;
}

static bool imuRead(uint8_t reg, uint8_t *val) {
  if (!i2cStart()) return false;
  if (!i2cWriteByte((uint8_t)(imuAddr << 1)) || !i2cWriteByte(reg)) {
    i2cStop(); return false;
  }
  if (!i2cStart()) { i2cStop(); return false; }       // repeated start
  if (!i2cWriteByte((uint8_t)((imuAddr << 1) | 1))) {
    i2cStop(); return false;
  }
  bool ok = i2cReadByte(val, false);
  i2cStop();
  return ok;
}

/* True if an LSM6DS3TR-C answered and double-tap is armed. */
static bool imuTapBegin() {
  pinMode(PIN_LSM6DS3TR_C_POWER, OUTPUT);
#if defined(NRF52840_XXAA)
  // The IMU supply pin is P1.08 and MUST be in H0H1 high-drive mode. Standard
  // drive cannot source enough current to power the sensor, so the part simply
  // never comes up and every I2C probe reads back 0xFF. Matches what Seeed's
  // own LSM6DS3 library does; nothing in the variant header hints at it.
  NRF_P1->PIN_CNF[8] =
      ((uint32_t)GPIO_PIN_CNF_DIR_Output      << GPIO_PIN_CNF_DIR_Pos)   |
      ((uint32_t)GPIO_PIN_CNF_INPUT_Disconnect<< GPIO_PIN_CNF_INPUT_Pos) |
      ((uint32_t)GPIO_PIN_CNF_PULL_Disabled   << GPIO_PIN_CNF_PULL_Pos)  |
      ((uint32_t)GPIO_PIN_CNF_DRIVE_H0H1      << GPIO_PIN_CNF_DRIVE_Pos) |
      ((uint32_t)GPIO_PIN_CNF_SENSE_Disabled  << GPIO_PIN_CNF_SENSE_Pos);
#endif
  digitalWrite(PIN_LSM6DS3TR_C_POWER, HIGH);
  delay(50);                                    // sensor boot time

  pinMode(PIN_LSM6DS3TR_C_INT1, INPUT);

  const uint8_t buses[2][2] = {
    { PIN_WIRE1_SDA, PIN_WIRE1_SCL },   // 17/16, dedicated internal bus
    { PIN_WIRE_SDA,  PIN_WIRE_SCL  },   // 4/5,  D4/D5 header
  };

  uint8_t who = 0;
  imuAddr = 0;

  // Power-pin polarity is not documented consistently; try both.
  for (int pol = 0; pol < 2 && !imuAddr; pol++) {
    digitalWrite(PIN_LSM6DS3TR_C_POWER, pol == 0 ? HIGH : LOW);
    delay(60);
    for (int b = 0; b < 2 && !imuAddr; b++) {
      imuSda = buses[b][0];
      imuScl = buses[b][1];
      imuBus = (uint8_t)(b + 1);
      sdaHigh(); sclHigh();
      delay(5);
      for (int ai = 0; ai < 2; ai++) {
        imuAddr = (uint8_t)(IMU_ADDR_A + ai);
        who = 0xEE;
        bool ok = imuRead(REG_WHO_AM_I, &who);
        if (pol == 0) imuProbe[b * 2 + ai] = ok ? who : 0xFF;
        // LSM6DS3 reports 0x69, LSM6DS3TR-C reports 0x6A.
        if (ok && (who == 0x6A || who == 0x69)) { imuPwrUsed = (uint8_t)pol; break; }
        imuAddr = 0;
      }
    }
  }
  if (!imuAddr) {
    imuBus = 0;
    digitalWrite(PIN_LSM6DS3TR_C_POWER, HIGH);
    return false;
  }

  imuWrite(REG_CTRL1_XL,    0x60);  // 416 Hz, +/-2 g -- tap needs a high ODR
  // LIR (bit0) latches the event until TAP_SRC is read. Without it INT1 only
  // pulses for a few ms, which a 20 ms main loop misses almost every time.
  imuWrite(REG_TAP_CFG,     0x8F);  // interrupts on, tap X/Y/Z, latched
  imuWrite(REG_TAP_THS_6D,  0x85);  // Seeed demo value: tap threshold 5
  imuWrite(REG_INT_DUR2,    0x7F);  // gap/quiet/shock windows reject bumps
  imuWrite(REG_WAKE_UP_THS, 0x80);  // SINGLE_DOUBLE_TAP: double-tap mode
  imuWrite(REG_MD1_CFG,     0x08);  // route double-tap to INT1
  return true;
}

/* Poll the latched status register. INT1 (P0.11) is checked first as a cheap
 * fast path, but the register is read regardless every few loops: relying on
 * the pin alone drops taps whose pulse lands between polls. */
static bool imuDoubleTap() {
  if (!imuAddr) return false;

  // Rate-limit by wall clock, not loop iterations. While streaming, loop()
  // spins fast enough that "every 4th call" meant ~250 I2C reads a second --
  // enough bus churn to destabilise the device. LIR latches the event, so a
  // 50 ms poll cannot miss a tap.
  static uint32_t lastPollMs = 0;
  bool intHigh = digitalRead(PIN_LSM6DS3TR_C_INT1) != LOW;
  if (intHigh) bump(&tapIntHighCount);
  uint32_t now = millis();
  if (!intHigh && (now - lastPollMs) < 50) return false;
  lastPollMs = now;

  uint8_t src = 0;
  if (!imuRead(REG_TAP_SRC, &src)) return false;   // reading clears the latch
  if (src & 0x40) { tapLastSrc = src; bump(&tapIaCount); }
  if (src & 0x20) bump(&tapSingleCount);
  if (src & 0x10) bump(&tapDoubleCount);
  return (src & 0x10) != 0;                        // DOUBLE_TAP
}

/* Read back the config we wrote, plus live Z acceleration. If the writes did
 * not stick, or the accelerometer is not sampling, tap detection cannot work
 * no matter how the tap registers are tuned. */
static void imuReadback(uint8_t *out6, int16_t *accelZ) {
  const uint8_t regs[6] = { REG_CTRL1_XL, REG_TAP_CFG, REG_TAP_THS_6D,
                            REG_INT_DUR2, REG_WAKE_UP_THS, REG_MD1_CFG };
  for (int i = 0; i < 6; i++) {
    uint8_t v = 0;
    out6[i] = imuRead(regs[i], &v) ? v : 0xFF;
  }
  uint8_t lo = 0, hi = 0;
  if (imuRead(0x2C, &lo) && imuRead(0x2D, &hi))
    *accelZ = (int16_t)((uint16_t)lo | ((uint16_t)hi << 8));
  else
    *accelZ = 0;
}

static void imuSampleShock() {
  if (!imuAddr) return;
  uint8_t lo = 0, hi = 0;
  if (!imuRead(0x2C, &lo) || !imuRead(0x2D, &hi)) return;
  int16_t z = (int16_t)((uint16_t)lo | ((uint16_t)hi << 8));
  int32_t dev = z - 16384;                 // 16384 LSB ~= 1 g at +/-2 g
  if (dev < 0) dev = -dev;
  if ((uint16_t)dev > accelPeak) accelPeak = (uint16_t)dev;
}

static uint8_t accelPeakByte() {
  uint16_t v = accelPeak >> 8;
  return v > 255 ? 255 : (uint8_t)v;
}
static void accelPeakReset() { accelPeak = 0; }

static uint8_t tapDiag(int i) {
  switch (i) {
    case 0: return tapIaCount;
    case 1: return tapSingleCount;
    case 2: return tapDoubleCount;
    case 3: return tapLastSrc;
    default: return tapIntHighCount;
  }
}

static void imuSetTapThreshold(uint8_t t) {
  if (imuAddr) imuWrite(REG_TAP_THS_6D, (uint8_t)(t & 0x1F));
}

/* Exposed so the host can confirm the sensor answered. */
static uint8_t imuAddress() { return imuAddr; }
static uint8_t imuWhichBus() { return imuBus; }
static const uint8_t *imuProbeResults() { return imuProbe; }
static uint8_t imuPowerPolarity() { return imuPwrUsed; }

#endif
