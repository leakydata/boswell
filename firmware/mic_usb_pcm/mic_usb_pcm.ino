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

// 512 samples @ 16 kHz = 32 ms per frame — a comfortable margin for the
// USB write in loop() to finish before the next PDM callback lands.
static int16_t  sampleBuffer[512];
volatile int    samplesRead = 0;
volatile uint16_t drops     = 0;
static uint16_t seq         = 0;

void onPDMdata() {
  int bytesAvailable = PDM.available();

  // loop() has not consumed the previous frame yet; it is about to be
  // overwritten. Count it rather than silently losing time from the stream.
  if (samplesRead != 0) {
    drops++;
  }

  PDM.read(sampleBuffer, bytesAvailable);
  samplesRead = bytesAvailable / 2;
}

void setup() {
  Serial.begin(115200);   // baud is ignored on USB CDC
  while (!Serial) { }

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);   // XIAO LEDs are active-low: HIGH = off

  PDM.onReceive(onPDMdata);
  PDM.setGain(MIC_GAIN);

  if (!PDM.begin(CHANNELS, FREQUENCY)) {
    // No framing magic here, so the host reports it as sync garbage rather
    // than mistaking it for audio.
    Serial.println("ERR: PDM.begin failed");
    while (1) { }
  }
}

void loop() {
  if (samplesRead == 0) {
    return;
  }

  noInterrupts();
  int      n = samplesRead;
  uint16_t d = drops;
  interrupts();

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
  Serial.write((const uint8_t *)sampleBuffer, n * 2);

  seq++;

  // Heartbeat so it is obvious at a glance that audio is flowing.
  digitalWrite(LED_BUILTIN, (seq & 0x10) ? LOW : HIGH);

  samplesRead = 0;
}
