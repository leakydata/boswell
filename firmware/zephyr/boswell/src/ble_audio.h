#ifndef BOSWELL_BLE_AUDIO_H
#define BOSWELL_BLE_AUDIO_H

#include "proto.h"

typedef void (*ctrl_handler_t)(uint8_t op, uint8_t arg);

int  ble_audio_init(ctrl_handler_t on_ctrl);
bool ble_audio_connected(void);
int  ble_audio_send(const uint8_t *frame, uint16_t len);
void ble_audio_publish_info(void);

#endif
