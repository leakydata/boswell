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
};

struct boswell_state {
    bool     streaming;
    bool     use16k;
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
