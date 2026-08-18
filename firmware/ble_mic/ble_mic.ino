/*
 * Phase 2 — XIAO nRF52840 Sense: PDM mic -> IMA ADPCM -> BLE notify.
 *
 * Built for the Seeeduino:nrf52 (SoftDevice S140 / Bluefruit) core rather
 * than the mbed core, because this needs MTU and connection-interval control
 * that ArduinoBLE does not expose.
 *
 * Bandwidth budget:
 *    8 kHz ADPCM  ->  32 kbps   fits a Bluetooth 4.0 host (CSR dongle)
 *   16 kHz ADPCM  ->  64 kbps   wants Bluetooth 5.x (AX210)
 *
 * Default is 8 kHz so it works on the BT4.0 dongle today; flip via the
 * control characteristic or SAMPLE_16K below once a BT5 adapter is in.
 *
 * GATT:
 *   service 4b1a0001-8f2c-4d5e-9a3b-1c7e6f8d0a21
 *     audio  4b1a0002-...  notify  ADPCM frames
 *     ctrl   4b1a0003-...  write   [0]=cmd [1]=arg
 *     info   4b1a0004-...  read    stream parameters
 *
 * Control commands:
 *   0x01 <0|1>    stop / start streaming
 *   0x02 <0|1>    sample rate: 0 = 8 kHz, 1 = 16 kHz
 *   0x03 <gain>   PDM gain, 0..80
 *   0x04 <0|1>    VAD gating off / on
 *   0x05 <n>      VAD threshold = n * 32 (RMS)
 *
 * Audio frame (little-endian):
 *   [0] seq       u16
 *   [2] flags     u8   bit0 16kHz, bit1 VAD-active, bit2 VAD-enabled
 *   [3] stepIndex u8   ADPCM state for THIS frame
 *   [4] predictor i16  ADPCM state for THIS frame
 *   [6] nsamples  u16
 *   [8] nibbles   nsamples/2 bytes
 */

#include <bluefruit.h>
#include <PDM.h>
#include "ima_adpcm.h"

// ---------------------------------------------------------------- config

#define FRAME_MS        20            // 50 frames/sec
#define PDM_RATE        16000         // PDM always runs at 16 kHz
#define RING_SAMPLES    4096          // ~256 ms at 16 kHz

static const int  DEFAULT_GAIN   = 50;   // +5 dB; measured ~26% peak in-room
static const bool DEFAULT_16K    = false;   // start at 8 kHz for BT 4.0
static const bool DEFAULT_VAD    = false;   // stream everything until tuned
static const int  DEFAULT_VAD_TH = 1120;  // measured: otsu split +3 dB

// Measured speech/silence separation in a normal room is only ~9 dB, so a
// bare threshold chops word onsets and endings. Hangover holds the gate open
// after speech drops below threshold; pre-roll back-fills the frames just
// before it opened, which is where quiet consonants live.
#define VAD_HANG_FRAMES  15       // 300 ms release
#define PREROLL_FRAMES    3       // 60 ms back-fill

// 320 samples = 20 ms @ 16 kHz; 160 bytes of nibbles + 8 byte header
#define MAX_SAMPLES     (PDM_RATE / 1000 * FRAME_MS)
#define HEADER_LEN      8
#define MAX_FRAME_LEN   (HEADER_LEN + MAX_SAMPLES / 2)

// ---------------------------------------------------------------- uuids
// 128-bit UUIDs are little-endian on the wire, so these are byte-reversed.
static const uint8_t UUID_SERVICE[16] = {
  0x21,0x0a,0x8d,0x6f,0x7e,0x1c,0x3b,0x9a,0x5e,0x4d,0x2c,0x8f,0x01,0x00,0x1a,0x4b };
static const uint8_t UUID_AUDIO[16] = {
  0x21,0x0a,0x8d,0x6f,0x7e,0x1c,0x3b,0x9a,0x5e,0x4d,0x2c,0x8f,0x02,0x00,0x1a,0x4b };
static const uint8_t UUID_CTRL[16] = {
  0x21,0x0a,0x8d,0x6f,0x7e,0x1c,0x3b,0x9a,0x5e,0x4d,0x2c,0x8f,0x03,0x00,0x1a,0x4b };
static const uint8_t UUID_INFO[16] = {
  0x21,0x0a,0x8d,0x6f,0x7e,0x1c,0x3b,0x9a,0x5e,0x4d,0x2c,0x8f,0x04,0x00,0x1a,0x4b };

BLEService        audioService(UUID_SERVICE);
BLECharacteristic audioChar(UUID_AUDIO);
BLECharacteristic ctrlChar(UUID_CTRL);
BLECharacteristic infoChar(UUID_INFO);

// ---------------------------------------------------------------- state

static int16_t  ring[RING_SAMPLES];
static volatile uint32_t ringHead = 0;     // written by the PDM callback
static volatile uint32_t ringTail = 0;     // read by loop()
static volatile uint32_t ringOverruns = 0;

static int16_t  pdmChunk[512];
static int16_t  frameSamples[MAX_SAMPLES];
static uint8_t  frameBuf[MAX_FRAME_LEN];

static bool     streaming   = false;
static bool     use16k      = DEFAULT_16K;
static bool     vadEnabled  = DEFAULT_VAD;
static int      vadThresh   = DEFAULT_VAD_TH;
static uint16_t seq         = 0;
static bool     connected   = false;

// Decimation carry: a leftover odd sample between frames when running 8 kHz.
static int      vadHangover = 0;
static int16_t  preRoll[PREROLL_FRAMES][MAX_SAMPLES];
static uint16_t preRollSeq[PREROLL_FRAMES];
static int      preRollLen[PREROLL_FRAMES];
static int      preRollCount = 0;
static int      preRollHead  = 0;

// ---------------------------------------------------------------- pdm

void onPDMdata() {
  int bytes = PDM.available();
  if (bytes <= 0) return;
  if (bytes > (int)sizeof(pdmChunk)) bytes = sizeof(pdmChunk);

  PDM.read(pdmChunk, bytes);
  int n = bytes / 2;

  uint32_t head = ringHead;
  for (int i = 0; i < n; i++) {
    uint32_t next = (head + 1) % RING_SAMPLES;
    if (next == ringTail) {       // full: drop the oldest rather than stall
      ringOverruns++;
      break;
    }
    ring[head] = pdmChunk[i];
    head = next;
  }
  ringHead = head;
}

static inline uint32_t ringAvailable() {
  uint32_t h = ringHead, t = ringTail;
  return (h >= t) ? (h - t) : (RING_SAMPLES - t + h);
}

// ---------------------------------------------------------------- helpers

static void applyGain(int gain) {
  // NOTE: PDM.begin() internally calls setGain(DEFAULT_PDM_GAIN=20), so gain
  // must always be set AFTER begin(), never before. GAINL/GAINR is writable
  // while the peripheral is running, so no restart is needed here.
  PDM.setGain(gain);
}

static void publishInfo() {
  uint8_t info[6];
  info[0] = 1;                          // codec: 1 = IMA ADPCM
  info[1] = use16k ? 1 : 0;
  info[2] = FRAME_MS;
  uint16_t ns = use16k ? MAX_SAMPLES : MAX_SAMPLES / 2;
  info[3] = ns & 0xFF;
  info[4] = (ns >> 8) & 0xFF;
  info[5] = vadEnabled ? 1 : 0;
  infoChar.write(info, sizeof(info));
}

void ctrl_write_cb(uint16_t handle, BLECharacteristic *chr,
                   uint8_t *data, uint16_t len) {
  (void)handle; (void)chr;
  if (len < 2) return;

  switch (data[0]) {
    case 0x01:
      streaming = data[1] != 0;
      vadHangover = 0;
      preRollCount = 0;
      // Stale audio would be sent as if it were live; start from now.
      ringTail = ringHead;
      break;
    case 0x02:
      use16k = data[1] != 0;
      break;
    case 0x03:
      applyGain(data[1] > 80 ? 80 : data[1]);
      break;
    case 0x04:
      vadEnabled = data[1] != 0;
      break;
    case 0x05:
      vadThresh = data[1] * 32;
      break;
  }
  publishInfo();
}

void connect_cb(uint16_t handle) {
  BLEConnection *conn = Bluefruit.Connection(handle);
  connected = true;
  // 244 bytes of ATT payload covers the largest 16 kHz frame in one packet.
  conn->requestMtuExchange(247);
  conn->requestConnectionParameter(6, 12);   // 7.5–15 ms, units of 1.25 ms
  digitalWrite(LED_BUILTIN, LOW);            // active low: on
}

void disconnect_cb(uint16_t handle, uint8_t reason) {
  (void)handle; (void)reason;
  connected = false;
  streaming = false;
  digitalWrite(LED_BUILTIN, HIGH);
}


// ---------------------------------------------------------------- framing

static void sendFrame(const int16_t *samples, int n, uint16_t sq, bool voiced) {
  AdpcmState st = { samples[0], 0 };
  int16_t predictor0 = (int16_t)st.predictor;
  uint8_t index0     = (uint8_t)st.index;

  adpcm_encode_block(samples, n, frameBuf + HEADER_LEN, &st);

  uint8_t flags = 0;
  if (use16k)     flags |= 0x01;
  if (voiced)     flags |= 0x02;
  if (vadEnabled) flags |= 0x04;

  frameBuf[0] = sq & 0xFF;
  frameBuf[1] = (sq >> 8) & 0xFF;
  frameBuf[2] = flags;
  frameBuf[3] = index0;
  frameBuf[4] = predictor0 & 0xFF;
  frameBuf[5] = (predictor0 >> 8) & 0xFF;
  frameBuf[6] = n & 0xFF;
  frameBuf[7] = (n >> 8) & 0xFF;

  audioChar.notify(frameBuf, HEADER_LEN + n / 2);
}

static void stashPreRoll(const int16_t *samples, int n, uint16_t sq) {
  memcpy(preRoll[preRollHead], samples, n * sizeof(int16_t));
  preRollSeq[preRollHead] = sq;
  preRollLen[preRollHead] = n;
  preRollHead = (preRollHead + 1) % PREROLL_FRAMES;
  if (preRollCount < PREROLL_FRAMES) preRollCount++;
}

/* Emit the buffered pre-speech frames oldest-first, so their sequence numbers
 * stay contiguous with the frame that opened the gate. */
static void flushPreRoll() {
  int start = (preRollHead - preRollCount + PREROLL_FRAMES) % PREROLL_FRAMES;
  for (int i = 0; i < preRollCount; i++) {
    int idx = (start + i) % PREROLL_FRAMES;
    sendFrame(preRoll[idx], preRollLen[idx], preRollSeq[idx], true);
  }
  preRollCount = 0;
}

// ---------------------------------------------------------------- setup

void setup() {
  Serial.begin(115200);

  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);

  PDM.onReceive(onPDMdata);
  if (!PDM.begin(1, PDM_RATE)) {
    Serial.println("ERR: PDM.begin failed");
    while (1) { delay(100); }
  }
  PDM.setGain(DEFAULT_GAIN);   // must follow begin() -- see applyGain()

  // Must precede begin(): raises the SoftDevice event length and queue depth,
  // without which sustained notify throughput collapses.
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(1, 0);
  Bluefruit.setTxPower(4);
  Bluefruit.setName("XIAO-MIC");
  Bluefruit.Periph.setConnectCallback(connect_cb);
  Bluefruit.Periph.setDisconnectCallback(disconnect_cb);

  audioService.begin();

  audioChar.setProperties(CHR_PROPS_NOTIFY);
  audioChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  audioChar.setMaxLen(MAX_FRAME_LEN);
  audioChar.begin();

  ctrlChar.setProperties(CHR_PROPS_WRITE | CHR_PROPS_WRITE_WO_RESP);
  ctrlChar.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  ctrlChar.setMaxLen(2);
  ctrlChar.setWriteCallback(ctrl_write_cb);
  ctrlChar.begin();

  infoChar.setProperties(CHR_PROPS_READ);
  infoChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  infoChar.setMaxLen(6);
  infoChar.begin();
  publishInfo();

  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(audioService);
  Bluefruit.ScanResponse.addName();
  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);

  Serial.println("XIAO-MIC advertising");
}

// ---------------------------------------------------------------- loop

void loop() {
  if (!connected || !streaming) {
    ringTail = ringHead;      // stay live rather than accumulating stale audio
    delay(5);
    return;
  }

  const int outSamples = use16k ? MAX_SAMPLES : MAX_SAMPLES / 2;
  const uint32_t need  = use16k ? (uint32_t)MAX_SAMPLES : (uint32_t)MAX_SAMPLES;

  if (ringAvailable() < need) {
    delay(1);
    return;
  }

  // Pull one frame, decimating 2:1 when running at 8 kHz. Averaging pairs is
  // a 2-tap FIR with its first null at 4 kHz -- crude, but its stopband sits
  // where the decimation folds. Proper Opus at 16 kHz supersedes this.
  uint32_t tail = ringTail;
  if (use16k) {
    for (int i = 0; i < outSamples; i++) {
      frameSamples[i] = ring[tail];
      tail = (tail + 1) % RING_SAMPLES;
    }
  } else {
    for (int i = 0; i < outSamples; i++) {
      int32_t a = ring[tail];
      tail = (tail + 1) % RING_SAMPLES;
      int32_t b = ring[tail];
      tail = (tail + 1) % RING_SAMPLES;
      frameSamples[i] = (int16_t)((a + b) / 2);
    }
  }
  ringTail = tail;

  // Energy VAD. Hangover + pre-roll compensate for the weak separation.
  bool voiced = true;
  if (vadEnabled) {
    uint64_t acc = 0;
    for (int i = 0; i < outSamples; i++) {
      int32_t v = frameSamples[i];
      acc += (uint64_t)(v * v);
    }
    uint32_t rms = (uint32_t)sqrt((double)(acc / outSamples));

    if (rms >= (uint32_t)vadThresh) {
      if (vadHangover == 0) flushPreRoll();   // gate opening: back-fill first
      vadHangover = VAD_HANG_FRAMES;
    } else if (vadHangover > 0) {
      vadHangover--;
    }
    voiced = vadHangover > 0;

    if (!voiced) {
      stashPreRoll(frameSamples, outSamples, seq);
      seq++;      // keep advancing so the host reads the gap as silence
      return;
    }
  }

  sendFrame(frameSamples, outSamples, seq, voiced);
  seq++;
}
