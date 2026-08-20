/*
 * Wire protocol, shared with the Arduino build so the host is unaffected.
 *
 *   [0] seq       u16
 *   [2] flags     u8   bit0 16kHz, bit1 voiced, bit2 VAD on, bit3 from flash
 *   [3] stepIndex u8   ADPCM state for THIS frame
 *   [4] predictor i16  ADPCM state for THIS frame
 *   [6] nsamples  u16
 *   [8] t_ms      u32  uptime at capture
 *  [12] nibbles   nsamples/2 bytes
 *
 * Every frame carries its own ADPCM state, so a lost frame costs one frame
 * rather than desynchronising the decoder for the rest of the stream.
 */
#ifndef BOSWELL_PROTO_H
#define BOSWELL_PROTO_H

#include <zephyr/kernel.h>

#define PROTO_HEADER_LEN   12
#define PROTO_FRAME_MS     20
#define PDM_RATE           16000
#define MAX_SAMPLES        (PDM_RATE / 1000 * PROTO_FRAME_MS)   /* 320 */
#define MAX_FRAME_LEN      (PROTO_HEADER_LEN + MAX_SAMPLES / 2)

#define FLAG_16K       0x01
#define FLAG_VOICED    0x02
#define FLAG_VAD_ON    0x04
#define FLAG_FROM_FLASH 0x08

/* 128-bit UUIDs, byte-reversed as they go on the wire.
 * service 4b1a0001-8f2c-4d5e-9a3b-1c7e6f8d0a21 */
#define BOSWELL_UUID_SERVICE \
    BT_UUID_128_ENCODE(0x4b1a0001, 0x8f2c, 0x4d5e, 0x9a3b, 0x1c7e6f8d0a21)
#define BOSWELL_UUID_AUDIO \
    BT_UUID_128_ENCODE(0x4b1a0002, 0x8f2c, 0x4d5e, 0x9a3b, 0x1c7e6f8d0a21)
#define BOSWELL_UUID_CTRL \
    BT_UUID_128_ENCODE(0x4b1a0003, 0x8f2c, 0x4d5e, 0x9a3b, 0x1c7e6f8d0a21)
#define BOSWELL_UUID_INFO \
    BT_UUID_128_ENCODE(0x4b1a0004, 0x8f2c, 0x4d5e, 0x9a3b, 0x1c7e6f8d0a21)
/* Raw motion. Sampled on the device and worked out on the host, the same way
 * the audio is: what counts as a step, or a gesture, or sitting down is a
 * question better answered where it can be changed without reflashing. */
#define BOSWELL_UUID_IMU \
    BT_UUID_128_ENCODE(0x4b1a0005, 0x8f2c, 0x4d5e, 0x9a3b, 0x1c7e6f8d0a21)

/* IMU frame: [seq:u16][flags:u8][count:u8][hz:u16][t_ms:u32] then `count`
 * samples of int16 x,y,z -- accelerometer, and gyroscope too when asked.
 *
 * Accel-only is about 10 uA on this part and the gyroscope is about 0.9 mA,
 * roughly a hundred times more, so the gyroscope is off unless something
 * asks for it. */
#define IMU_HEADER_LEN   10
#define IMU_FLAG_GYRO    0x01
/* Gyroscope full scale, in bits 1-2, so the host converts with the range the
 * device actually configured. The firmware selected 500 dps and the host
 * divided by 2000, so every recorded rotation was out by a factor of four --
 * a disagreement that no amount of care on either side alone would catch. */
#define IMU_GYRO_FS_SHIFT 1
#define IMU_GYRO_FS_MASK  0x06
#define IMU_GYRO_FS_250   0
#define IMU_GYRO_FS_500   1
#define IMU_GYRO_FS_1000  2
#define IMU_GYRO_FS_2000  3
#define IMU_MAX_SAMPLES  10

/* ---- info characteristic layout -------------------------------------------
 *
 * Forty bytes, read by the host to learn what the device is and what it is
 * doing. Both firmwares publish it and they do NOT agree on every field:
 * Arduino puts tap diagnostics in 13-26, Zephyr puts step count and motion in
 * 13-17. A host reading byte 13 cannot know which it got without being told.
 *
 * Hence bytes 18-21, which are free in both. Byte 18 is the layout version:
 * a host seeing 0 is talking to firmware from before this existed and should
 * treat everything outside the common core as unknown rather than as zero.
 *
 *   0     codec (1 = IMA ADPCM)
 *   1     16 kHz flag
 *   2     frame milliseconds
 *   3-4   samples per frame
 *   5     bit0 VAD on, bit1 backlog mode, bit2 capture running
 *   6     IMU bus (0 = absent)
 *   7     IMU address
 *   8-11  WHO_AM_I probe results        (Arduino: four slots; Zephyr: two)
 *   12    IMU power polarity            (Arduino only)
 *   13-17 steps and motion flags        (ZEPHYR ONLY -- Arduino: tap counters)
 *   18    info layout version           <- read this first
 *   19    firmware identity
 *   20-21 capability bits
 *   22-23 reserved
 *   24-26 accelerometer sample          (Arduino only)
 *   27-31 QSPI ready, pending, capacity
 *   32-33 LED level and mode
 *   34-37 battery mV, percent, flags
 *   38    ring overruns                 (Arduino only; Zephyr has no equivalent)
 *   39    radio transmit power
 */
#define INFO_VERSION      1

#define INFO_FW_ARDUINO   1
#define INFO_FW_ZEPHYR    2

#define INFO_CAP_STEPS     0x0001   /* bytes 13-17 are steps and motion */
#define INFO_CAP_IMU_RAW   0x0002   /* raw motion characteristic exists */
#define INFO_CAP_FLASH     0x0004   /* store-and-forward */
#define INFO_CAP_OTA       0x0008   /* CTRL_DFU accepts the Bluetooth variant */
#define INFO_CAP_TAP_DIAG  0x0010   /* bytes 13-26 are tap diagnostics */
#define INFO_CAP_OVERRUNS  0x0020   /* byte 38 is meaningful */
#define INFO_CAP_STATE     0x0040   /* byte 5 bit 2 is the real capture state */

/* Control opcodes, unchanged from the Arduino build. */
enum {
    CTRL_STREAM       = 0x01,
    CTRL_RATE         = 0x02,
    CTRL_GAIN         = 0x03,
    CTRL_VAD          = 0x04,
    CTRL_VAD_THRESH   = 0x05,
    CTRL_TAP_ENABLE   = 0x06,
    CTRL_TAP_THRESH   = 0x07,
    CTRL_CLEAR_BUFFER = 0x08,
    CTRL_BACKLOG_MODE = 0x09,
    CTRL_LED_LEVEL    = 0x0A,
    CTRL_LED_MODE     = 0x0B,
    CTRL_FAST_CHARGE  = 0x0C,
    CTRL_MIC_SAVE     = 0x0D,
    CTRL_TX_POWER     = 0x0E,
    /* Reboot into the bootloader. Argument must be 0x5A so a stray write
     * cannot take the device offline. */
    CTRL_DFU          = 0x0F,
    CTRL_IMU_STREAM   = 0x10,   /* 0 off, else samples per second */
    CTRL_IMU_GYRO     = 0x11,   /* the expensive half; off by default */
};

struct boswell_state {
    bool     streaming;
    bool     use16k;
    uint8_t  imu_hz;        /* 0 = off */
    bool     vad_enabled;
    uint16_t vad_thresh;
    uint8_t  gain;
    uint8_t  led_level;
    uint8_t  led_mode;
    uint8_t  backlog_mode;
    uint8_t  mic_power_save;
    int8_t   tx_power;
};

extern struct boswell_state g_state;

#endif
