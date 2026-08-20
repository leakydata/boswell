/*
 * CRC-8 over a backlog record's payload.
 *
 * In its own header so the same definition can be compiled natively and
 * tested on a machine, rather than only ever running on the board. Every
 * firmware fault this project has found needed a flash, a reconnect and a
 * shell session to see; the parts that are pure arithmetic do not have to be
 * like that.
 *
 * Polynomial 0x07, init 0xFF. This guards against a read cursor landing
 * mid-record, not against a hostile writer, so eight bits is enough: it turns
 * a one-in-256 coincidence into a one-in-65536 one.
 */
#ifndef BOSWELL_REC_CRC_H
#define BOSWELL_REC_CRC_H

#include <stdint.h>

static inline uint8_t rec_crc8(const uint8_t *data, uint8_t len)
{
    uint8_t crc = 0xFF;

    for (uint8_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int b = 0; b < 8; b++) {
            crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
        }
    }
    return crc;
}

#endif
