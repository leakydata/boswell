/*
 * Phase 1 — XIAO nRF52840 Sense: PDM mic -> framed 16 kHz PCM over USB CDC.
 *
 * Deliberately throwaway: this exists to prove the microphone and the audio
 * path work, with no radio in the picture. The BLE work happens on the
 * Seeeduino:nrf52 (SoftDevice) core, not here.
 *
 * Wire format, little-endian, repeated forever:
 *   0xA5 0x5A | seq:u16 | drops:u16 | nsamples:u16 | int16 pcm[nsamples]
 *
 * `drops` is cumulative buffer overruns since boot. It stays at 0 when the
 * host keeps up; a climbing value means the host read loop is too slow.
 */

#include <PDM.h>

static const char  CHANNELS  = 1;
static const int   FREQUENCY = 16000;

// Mic gain. 20 is the library default for a Sense board. 30 picks up
// conversational speech across a room without clipping close talk.
// Raise toward 50 for distant speakers, drop to ~20 if loud speech clips.
static const int   MIC_GAIN  = 30;

static const uint8_t MAGIC0 = 0xA5;
static const uint8_t MAGIC1 = 0x5A;

// 512 samples @ 16 kHz = 32 ms per frame.
//
// Two buffers, not one. The USB write in loop() runs with interrupts enabled
// and takes a while; a single buffer meant the next PDM callback could
// overwrite the samples that were still being transmitted, so a frame could
// go out with its first half from one moment and its second half from
// another. The drop counter would not move -- nothing was dropped -- and the
// audio would simply be wrong in a way nothing downstream could detect.
//
// The callback always fills the buffer loop() is not reading.
#define FRAME_SAMPLES 512
static int16_t  sampleBuffer[2][FRAME_SAMPLES];
volatile uint8_t fillIdx    = 0;   // the one the callback writes
volatile int    samplesRead = 0;   // samples waiting in 1 - fillIdx
volatile uint16_t drops     = 0;
volatile uint16_t oversize  = 0;   // reads larger than the buffer
static uint16_t seq         = 0;

void onPDMdata() {
  int bytesAvailable = PDM.available();

  // loop() has not consumed the previous frame yet; it is about to be
  // overwritten. Count it rather than silently losing time from the stream.
  if (samplesRead != 0) {
    drops++;
  }

  // Clamp. This went straight to PDM.read() and a driver offering more than
  // the buffer holds would have written past the end of it -- into whatever
  // followed, which here is the other buffer and the counters.
  if (bytesAvailable > (int)sizeof(sampleBuffer[0])) {
    oversize++;
    bytesAvailable = (int)sizeof(sampleBuffer[0]);
  }
  PDM.read(sampleBuffer[fillIdx], bytesAvailable);
  samplesRead = bytesAvailable / 2;
  fillIdx = 1 - fillIdx;           // loop() reads what was just filled
}

void setup() {
  Serial.begin(115200);   // baud is ignored on USB CDC
  // Wait for a host, but not forever. This blocked indefinitely with nothing
  // attached, which prevents headless use and looks exactly like a dead
  // board -- no LED, no serial, no way to tell it from a brick.
  for (uint32_t t0 = millis(); !Serial && millis() - t0 < 5000; ) {
    delay(10);
  }

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);   // XIAO LEDs are active-low: HIGH = off

  PDM.onReceive(onPDMdata);

  if (!PDM.begin(CHANNELS, FREQUENCY)) {
    // No framing magic here, so the host reports it as sync garbage rather
    // than mistaking it for audio.
    Serial.println("ERR: PDM.begin failed");
    while (1) { }
  }
  // After begin, not before: begin() resets the gain, so setting it first
  // meant MIC_GAIN was written and then immediately discarded. The BLE
  // firmware documents this; this sketch did the opposite of what it says.
  PDM.setGain(MIC_GAIN);
}

void loop() {
  if (samplesRead == 0) {
    return;
  }

  noInterrupts();
  int      n   = samplesRead;
  uint16_t d   = drops;
  uint16_t ov  = oversize;
  uint8_t  idx = 1 - fillIdx;      // the buffer the callback is not using
  samplesRead  = 0;                // claim it before transmitting
  interrupts();

  if (n > FRAME_SAMPLES) {
    n = FRAME_SAMPLES;
  }
  (void)ov;

  uint8_t header[8];
  header[0] = MAGIC0;
  header[1] = MAGIC1;
  header[2] = seq & 0xFF;
  header[3] = (seq >> 8) & 0xFF;
  header[4] = d & 0xFF;
  header[5] = (d >> 8) & 0xFF;
  header[6] = n & 0xFF;
  header[7] = (n >> 8) & 0xFF;

  Serial.write(header, sizeof(header));
  Serial.write((const uint8_t *)sampleBuffer[idx], n * 2);

  seq++;

  // Heartbeat so it is obvious at a glance that audio is flowing.
  digitalWrite(LED_BUILTIN, (seq & 0x10) ? LOW : HIGH);
}
