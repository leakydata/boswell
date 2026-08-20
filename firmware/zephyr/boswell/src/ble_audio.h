#ifndef BOSWELL_BLE_AUDIO_H
#define BOSWELL_BLE_AUDIO_H

#include "proto.h"

typedef void (*ctrl_handler_t)(uint8_t op, uint8_t arg);

int  ble_audio_init(ctrl_handler_t on_ctrl);
bool ble_audio_connected(void);
/* Connected *and* subscribed. A host that has connected but not enabled
 * notifications cannot receive audio, and buffering is the right move then
 * just as much as when nothing is connected at all. */
bool ble_audio_ready(void);
int  ble_audio_send(const uint8_t *frame, uint16_t len);
void ble_audio_send_stats(uint32_t out[4]);
void ble_audio_dead_link_stats(uint32_t out[3]);
/* The live connection's controller handle, or -1 when there is none. */
int  ble_audio_conn_handle(void);
void ble_audio_note_delivery_attempt(void);
/* Frames the radio would not take. Defined in main.c, published in info. */
uint32_t ble_audio_notify_drops(void);
void ble_audio_publish_info(void);

/* Re-negotiate the connection interval. Streaming wants a tight one; idling
 * does not. Must be called when streaming starts or stops, not just at
 * subscribe time -- the host subscribes before it sends CTRL_STREAM, so the
 * link would otherwise sit at the idle interval for the whole capture. */
void ble_audio_apply_conn_params(bool streaming);
/* Whether an advertiser is believed to be running, and a way to force one.
 * A peripheral that is neither connected nor advertising is unreachable
 * until it is power-cycled, which is the worst state this firmware can be
 * in, so it is made observable and recoverable. */
bool ble_audio_advertising(void);
/* A link exists, whether or not the host has subscribed to audio yet.
 * Distinct from ble_audio_connected(), which means "linked AND subscribed" --
 * conflating the two made a perfectly healthy connection look like the
 * unreachable state and sent a diagnosis off in the wrong direction. */
bool ble_audio_linked(void);
/* arms, fires, drops for the never-subscribed guard. A guard whose firing
 * cannot be observed is indistinguishable from one that is not there. */
void ble_audio_idle_stats(uint32_t out[3]);
/* Raw motion goes out on its own characteristic so a host that only wants
 * audio is not made to decode it, and so losing a motion frame can never
 * disturb the audio sequence. */
int  ble_imu_send(const uint8_t *frame, uint16_t len);
bool ble_imu_ready(void);
int  ble_audio_advertise_now(void);

#endif
