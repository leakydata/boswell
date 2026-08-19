#ifndef BOSWELL_BLE_AUDIO_H
#define BOSWELL_BLE_AUDIO_H

#include "proto.h"

typedef void (*ctrl_handler_t)(uint8_t op, uint8_t arg);

int  ble_audio_init(ctrl_handler_t on_ctrl);
bool ble_audio_connected(void);
int  ble_audio_send(const uint8_t *frame, uint16_t len);
void ble_audio_publish_info(void);

/* Re-negotiate the connection interval. Streaming wants a tight one; idling
 * does not. Must be called when streaming starts or stops, not just at
 * subscribe time -- the host subscribes before it sends CTRL_STREAM, so the
 * link would otherwise sit at the idle interval for the whole capture. */
void ble_audio_apply_conn_params(bool streaming);

#endif
