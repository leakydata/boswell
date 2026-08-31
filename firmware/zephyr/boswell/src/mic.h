#ifndef BOSWELL_MIC_H
#define BOSWELL_MIC_H

#include "proto.h"

int  mic_init(void);
int  mic_start(void);
void mic_stop(void);
void mic_set_gain(uint8_t gain);
bool mic_running(void);

/* Rebuild the PDM stream after it has wedged. The driver can reach a state
 * where every read fails forever while the device still thinks it is
 * capturing; a stop and start does not clear it, the stream has to be
 * configured again. */
int  mic_recover(void);

/* Pull one frame of PCM. Blocks up to `timeout` for the DMIC driver to hand
 * over a block. Returns the sample count, or 0 on timeout. */
int  mic_read_frame(int16_t *dst, int max_samples, k_timeout_t timeout);

#endif
