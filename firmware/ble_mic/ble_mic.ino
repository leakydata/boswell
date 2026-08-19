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
 *   [8] t_ms      u32  device uptime when this frame was captured
 *  [12] nibbles   nsamples/2 bytes
 *
 * The timestamp is what makes recovered audio land in the right place. Without
 * it the host can only stamp arrival time, so a conversation buffered to flash
 * at 2pm and drained at 4pm is filed as 4pm -- wrong for the one thing this
 * device exists to do.
 */

#include <bluefruit.h>
#include <Adafruit_LittleFS.h>
#include <InternalFileSystem.h>
#include <PDM.h>
#include "ima_adpcm.h"
#include "settings.h"
#include "imu_tap.h"
#include "qspi_store.h"

// ---------------------------------------------------------------- config

#define FRAME_MS        20            // 50 frames/sec
#define PDM_RATE        16000         // PDM always runs at 16 kHz
/* Writing to flash means erasing a sector every ~0.9 s of buffered audio, and
 * a NOR erase blocks the CPU for tens of milliseconds -- up to 300 ms on this
 * part. The microphone keeps producing samples throughout, so the ring has to
 * be able to absorb a whole erase or it overruns and the dropped samples are
 * audible as a click. At 256 ms of headroom it could not. 16384 samples is
 * just over a second at 16 kHz, which covers even a worst-case erase, and
 * costs 32 kB of the 210 kB free. */
#define RING_SAMPLES    16384         // ~1.02 s at 16 kHz

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
#define HEADER_LEN      12
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
static bool     micRunning  = true;
static int      currentGain = DEFAULT_GAIN;
static uint16_t seq         = 0;
static bool     connected   = false;

// Decimation carry: a leftover odd sample between frames when running 8 kHz.
static int      vadHangover = 0;
static int16_t  preRoll[PREROLL_FRAMES][MAX_SAMPLES];
static uint16_t preRollSeq[PREROLL_FRAMES];
static uint32_t preRollStamp[PREROLL_FRAMES];
static int      preRollLen[PREROLL_FRAMES];
static int      preRollCount = 0;
static int      preRollHead  = 0;

static bool     imuReady    = false;
static bool     tapEnabled  = true;
static uint32_t lastTapMs   = 0;

static bool     qspiOk      = false;
static uint8_t  drainBuf[MAX_FRAME_LEN];
static uint8_t  drainLen    = 0;
/* 0 = drain the backlog before any live audio (conversation stays in order)
 * 1 = live audio first, backlog trickled out alongside it (hear the present
 *     immediately; recovered audio arrives out of order and flagged). */
static uint8_t  backlogMode = 0;

/* LED power. Brightness is real PWM, so average current tracks duty cycle
 * almost exactly -- 10% brightness costs about 10% of the current. The PWM
 * peripheral itself draws roughly 50-100 uA though, so below a few percent it
 * costs more than the light does; that is what pulse mode is for. A steady LED
 * at full brightness is on the order of 2 mA, against a whole-device budget
 * near 8 mA, so this is worth having. */
static uint8_t  ledLevel = 255;      // 0 = off, 255 = full
/* Blink by default. A 25 ms flash every 3 s is under 1% duty with no PWM
 * running, against roughly 2 mA for a steady LED on a device budget near
 * 8 mA -- so leaving it lit costs a quarter of the power for an indicator
 * nobody watches most of the time. Steady is available, it is just not the
 * sensible default for something meant to run all day. */
static uint8_t  ledMode  = 1;        // 0 steady, 1 brief pulse
#define LED_PULSE_ON_MS    25
#define LED_PULSE_EVERY_MS 3000
static bool     ledWantR = false, ledWantG = false, ledWantB = false;

/* ---- battery ----
 * VBAT reaches the ADC through a 1M/510k divider, and VBAT_ENABLE must be
 * driven low first or the divider is disconnected and the reading is garbage.
 * The 3.0 V internal reference is used rather than VDD so the result does not
 * drift as the cell discharges. */
/* The variant names the charge-current pin but not the charge-status one.
 * From the board pin map: D23 is P0.17, the BQ25101 ~CHG output, active low. */
#ifndef PIN_CHG
#define PIN_CHG         (23)
#endif
#define VBAT_DIVIDER    (1510.0f / 510.0f)
#define VBAT_REF_MV     3000.0f
#define ADC_MAX         4095.0f
static uint16_t batteryMv   = 0;
static bool     charging    = false;
static bool     fastCharge  = false;   // BQ25101: LOW = 100 mA, float = 50 mA
static uint8_t  micPowerSave = 1;      // stop the mic when not armed

static uint16_t readBatteryMv() {
  digitalWrite(VBAT_ENABLE, LOW);      // connect the divider
  delayMicroseconds(200);
  uint32_t acc = 0;
  for (int i = 0; i < 8; i++) acc += analogRead(PIN_VBAT);
  digitalWrite(VBAT_ENABLE, HIGH);     // and disconnect it again
  float raw = acc / 8.0f;
  return (uint16_t)(raw * (VBAT_REF_MV / ADC_MAX) * VBAT_DIVIDER);
}

/* A single-cell lithium curve is flat through the middle, so a linear map from
 * volts to percent is misleading. These breakpoints track the discharge curve
 * closely enough to be useful without pretending to precision. */
static uint8_t batteryPercent(uint16_t mv) {
  static const uint16_t pts[][2] = {
    {4150, 100}, {4050, 90}, {3950, 75}, {3850, 60}, {3800, 50},
    {3750, 40}, {3700, 30}, {3650, 20}, {3550, 10}, {3400, 5}, {3200, 0}
  };
  if (mv >= pts[0][0]) return 100;
  for (size_t i = 1; i < sizeof(pts) / sizeof(pts[0]); i++) {
    if (mv >= pts[i][0]) {
      uint16_t hiV = pts[i - 1][0], loV = pts[i][0];
      uint8_t  hiP = pts[i - 1][1], loP = pts[i][1];
      return (uint8_t)(loP + (long)(mv - loV) * (hiP - loP) / (hiV - loV));
    }
  }
  return 0;
}

static void setFastCharge(bool on) {
  fastCharge = on;
  if (on) {
    pinMode(PIN_CHARGING_CURRENT, OUTPUT);
    digitalWrite(PIN_CHARGING_CURRENT, LOW);    // 100 mA
  } else {
    pinMode(PIN_CHARGING_CURRENT, INPUT);       // float: 50 mA default
  }
}

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

// ---------------------------------------------------------------- settings

/* Everything tunable lives here so it survives a reboot. Re-tuning gain and
 * thresholds every time the battery runs down is the kind of friction that
 * makes a device annoying to actually wear. */
static int8_t txPower = 4;
static uint32_t settingsDirtyAt = 0;

static void settingsSave() {
  Settings cfg = { SETTINGS_MAGIC, SETTINGS_VER, (uint8_t)currentGain,
                   (uint8_t)use16k, (uint8_t)vadEnabled, (uint16_t)vadThresh,
                   ledLevel, ledMode, backlogMode, micPowerSave,
                   (uint8_t)tapEnabled, (uint8_t)fastCharge, txPower };
  InternalFS.remove(SETTINGS_PATH);
  Adafruit_LittleFS_Namespace::File f(InternalFS);
  if (f.open(SETTINGS_PATH, Adafruit_LittleFS_Namespace::FILE_O_WRITE)) {
    f.write((const uint8_t *)&cfg, sizeof(cfg));
    f.close();
  }
}

/* Coalesce writes: a slider dragged across its range should cost one flash
 * write when it settles, not forty on the way. */
static void settingsTouch() { settingsDirtyAt = millis(); }

static void settingsService() {
  if (settingsDirtyAt && millis() - settingsDirtyAt > 3000) {
    settingsDirtyAt = 0;
    settingsSave();
  }
}

static bool settingsLoad(Settings *out) {
  Adafruit_LittleFS_Namespace::File f(InternalFS);
  if (!f.open(SETTINGS_PATH, Adafruit_LittleFS_Namespace::FILE_O_READ)) return false;
  bool ok = f.read((uint8_t *)out, sizeof(*out)) == (int)sizeof(*out);
  f.close();
  return ok && out->magic == SETTINGS_MAGIC && out->version == SETTINGS_VER;
}

// ---------------------------------------------------------------- leds

/* XIAO RGB LED is common-anode: LOW lights a channel, so PWM is inverted. */
static void ledChannel(uint8_t pin, bool want) {
  if (!want || ledLevel == 0) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, HIGH);                 // off, and no PWM running
  } else if (ledLevel >= 255) {
    pinMode(pin, OUTPUT);
    digitalWrite(pin, LOW);                  // full, still no PWM needed
  } else {
    analogWrite(pin, 255 - ledLevel);
  }
}

static void applyLed(bool r, bool g, bool b) {
  ledChannel(LED_RED, r);
  ledChannel(LED_GREEN, g);
  ledChannel(LED_BLUE, b);
}

/* Remember what the state *should* look like; pulse mode shows it briefly. */
static void setLed(bool r, bool g, bool b) {
  ledWantR = r; ledWantG = g; ledWantB = b;
  if (ledMode == 1) return;                  // the pulse timer drives it
  applyLed(r, g, b);
}

/* In pulse mode the LED is dark almost all the time: a 25 ms flash every 3 s
 * is under 1% duty, which beats any practical PWM level and still tells you
 * the device is alive and what it is doing. */
static void servicePulse() {
  if (ledMode != 1) return;
  static uint32_t last = 0;
  static bool lit = false;
  uint32_t now = millis();
  if (!lit && now - last >= LED_PULSE_EVERY_MS) {
    applyLed(ledWantR, ledWantG, ledWantB);
    lit = true;
    last = now;
  } else if (lit && now - last >= LED_PULSE_ON_MS) {
    applyLed(false, false, false);
    lit = false;
  }
}

/* Capture is "armed" independently of the link, so the LED has to show both:
 *   blue     advertising, not armed
 *   magenta  armed with no host -- buffering to flash
 *   cyan     connected, draining the backlog
 *   green    connected, streaming live
 *   red      connected, not armed
 */
static void updateLed() {
  if (!connected)                     setLed(streaming, false, true);
  else if (!streaming)                setLed(true, false, false);
  else if (qspiOk && qspiPendingBytes()) setLed(false, true, true);
  else                                setLed(false, true, false);
}

// ---------------------------------------------------------------- helpers

static void applyGain(int gain) {
  currentGain = gain;
  // NOTE: PDM.begin() internally calls setGain(DEFAULT_PDM_GAIN=20), so gain
  // must always be set AFTER begin(), never before. GAINL/GAINR is writable
  // while the peripheral is running, so no restart is needed here.
  PDM.setGain(gain);
}

static void publishInfo() {
  uint8_t info[40];
  info[0] = 1;                          // codec: 1 = IMA ADPCM
  info[1] = use16k ? 1 : 0;
  info[2] = FRAME_MS;
  uint16_t ns = use16k ? MAX_SAMPLES : MAX_SAMPLES / 2;
  info[3] = ns & 0xFF;
  info[4] = (ns >> 8) & 0xFF;
  info[5] = vadEnabled ? 1 : 0;
  info[6] = imuWhichBus();      // 0 none, 1 = Wire1 17/16, 2 = Wire 4/5
  info[7] = imuAddress();
  const uint8_t *pr = imuProbeResults();
  info[8]  = pr[0];   // bus1 @0x6A
  info[9]  = pr[1];   // bus1 @0x6B
  info[10] = pr[2];   // bus2 @0x6A
  info[11] = pr[3];   // bus2 @0x6B
  info[12] = imuPowerPolarity();
  for (int i = 0; i < 5; i++) info[13 + i] = tapDiag(i);
  uint8_t rb[6]; int16_t az = 0;
  imuReadback(rb, &az);
  for (int i = 0; i < 6; i++) info[18 + i] = rb[i];
  info[24] = (uint8_t)(az & 0xFF);
  info[25] = (uint8_t)((az >> 8) & 0xFF);
  info[26] = accelPeakByte();

  info[5] |= (uint8_t)(backlogMode << 1);   // bit1 carries the backlog mode
  info[27] = qspiOk ? 1 : 0;
  uint32_t pend = qspiOk ? qspiPendingBytes() : 0;
  info[28] = (uint8_t)(pend & 0xFF);
  info[29] = (uint8_t)((pend >> 8) & 0xFF);
  info[30] = (uint8_t)((pend >> 16) & 0xFF);
  info[31] = (uint8_t)(qspiOk ? (qspiSizeBytes() >> 16) : 0);   // MiB-ish
  info[32] = ledLevel;
  info[33] = ledMode;
  info[39] = (uint8_t)txPower;
  {
    uint32_t ro = ringOverruns;
    info[38] = (uint8_t)(ro > 255 ? 255 : ro);
  }
  info[34] = (uint8_t)(batteryMv & 0xFF);
  info[35] = (uint8_t)(batteryMv >> 8);
  info[36] = batteryPercent(batteryMv);
  info[37] = (uint8_t)((charging ? 1 : 0) | (fastCharge ? 2 : 0) | (micRunning ? 4 : 0));
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
      ringOverruns = 0;          // count per capture session, not since boot
      applyConnInterval();
      updateLed();
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
    case 0x06:
      tapEnabled = data[1] != 0;
      break;
    case 0x07:
      imuSetTapThreshold(data[1]);
      break;
    case 0x08:                      // discard whatever is buffered on flash
      if (qspiOk) qspiClear();
      drainLen = 0;
      break;
    case 0x09:
      backlogMode = data[1] ? 1 : 0;
      break;
    case 0x0E: {                     // radio transmit power, dBm
      int8_t p = (int8_t)data[1];
      txPower = p;
      Bluefruit.setTxPower(p);
      break;
    }
    case 0x0A:                       // brightness, 0 = off
      ledLevel = data[1];
      updateLed();
      break;
    case 0x0C:                       // 0 = 50 mA charge, 1 = 100 mA
      setFastCharge(data[1] != 0);
      break;
    case 0x0D:
      micPowerSave = data[1] ? 1 : 0;
      break;
    case 0x0B:                       // 0 = steady, 1 = pulse
      ledMode = data[1] ? 1 : 0;
      if (ledMode == 0) applyLed(ledWantR, ledWantG, ledWantB);
      else applyLed(false, false, false);
      updateLed();
      break;
  }
  settingsTouch();
  publishInfo();
}

/* Audio needs a tight connection interval; an idle link does not. Holding
 * 7.5-15 ms while disarmed spends radio power on nothing. */
static void applyConnInterval() {
  BLEConnection *conn = Bluefruit.Connection(0);
  if (!conn) return;
  if (streaming) conn->requestConnectionParameter(6, 12);      // 7.5-15 ms
  else           conn->requestConnectionParameter(80, 160);    // 100-200 ms
}

void connect_cb(uint16_t handle) {
  BLEConnection *conn = Bluefruit.Connection(handle);
  connected = true;
  // 244 bytes of ATT payload covers the largest 16 kHz frame in one packet.
  conn->requestMtuExchange(247);
  applyConnInterval();
  updateLed();
}

void disconnect_cb(uint16_t handle, uint8_t reason) {
  (void)handle; (void)reason;
  connected = false;
  // Deliberately does NOT disarm: staying armed across a dropped link is the
  // whole point of store-and-forward. Frames now go to flash instead.
  updateLed();
}


// ---------------------------------------------------------------- framing

/* Capture time for the frame being built. Set from millis() for live audio;
 * for a frame replayed from flash it is whatever was stored with it. */
static uint32_t frameStampMs = 0;

static uint16_t buildFrame(const int16_t *samples, int n, uint16_t sq, bool voiced) {
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
  uint32_t ts = frameStampMs;
  frameBuf[8]  = (uint8_t)(ts & 0xFF);
  frameBuf[9]  = (uint8_t)((ts >> 8) & 0xFF);
  frameBuf[10] = (uint8_t)((ts >> 16) & 0xFF);
  frameBuf[11] = (uint8_t)((ts >> 24) & 0xFF);

  return (uint16_t)(HEADER_LEN + n / 2);
}

/* Live to the host when there is one, otherwise into flash. */
static void emitFrame(const int16_t *samples, int n, uint16_t sq, bool voiced) {
  frameStampMs = millis();
  uint16_t len = buildFrame(samples, n, sq, voiced);
  if (connected) audioChar.notify(frameBuf, len);
  else if (qspiOk) qspiPush(frameBuf, (uint8_t)len);
}

/* Push buffered frames out a few at a time so draining never starves the
 * radio. A frame that fails to queue is kept, not dropped. */
static void drainBacklog() {
  for (int i = 0; i < 4; i++) {
    if (drainLen == 0) {
      drainLen = qspiPop(drainBuf, sizeof(drainBuf));
      if (drainLen == 0) return;
    }
    drainBuf[2] |= 0x08;          // flag: this frame came out of flash
    if (audioChar.notify(drainBuf, drainLen)) drainLen = 0;
    else return;
  }
}

/* One frame only, so live audio keeps its slot on the radio. */
static void drainOne() {
  if (drainLen == 0) {
    drainLen = qspiPop(drainBuf, sizeof(drainBuf));
    if (drainLen == 0) return;
  }
  drainBuf[2] |= 0x08;
  if (audioChar.notify(drainBuf, drainLen)) drainLen = 0;
}

static void stashPreRoll(const int16_t *samples, int n, uint16_t sq) {
  memcpy(preRoll[preRollHead], samples, n * sizeof(int16_t));
  preRollStamp[preRollHead] = millis();
  preRollSeq[preRollHead] = sq;
  preRollLen[preRollHead] = n;
  preRollHead = (preRollHead + 1) % PREROLL_FRAMES;
  if (preRollCount < PREROLL_FRAMES) preRollCount++;
}

/* Emit the buffered pre-speech frames oldest-first, so their sequence numbers
 * stay contiguous with the frame that opened the gate. */
static void sendFrame(const int16_t *samples, int n, uint16_t sq, bool voiced) {
  uint16_t len = buildFrame(samples, n, sq, voiced);
  if (connected) audioChar.notify(frameBuf, len);
  else if (qspiOk) qspiPush(frameBuf, (uint8_t)len);
}

static void flushPreRoll() {
  int start = (preRollHead - preRollCount + PREROLL_FRAMES) % PREROLL_FRAMES;
  for (int i = 0; i < preRollCount; i++) {
    int idx = (start + i) % PREROLL_FRAMES;
    emitFrame(preRoll[idx], preRollLen[idx], preRollSeq[idx], true);
  }
  preRollCount = 0;
}

// ---------------------------------------------------------------- setup

/* Two firmware hangs during development left the board dark with no way back
 * except a manual reset. A watchdog turns that into a two-second gap. The
 * timeout is generous so a slow flash erase or a DFU session is never mistaken
 * for a hang; the bootloader feeds it during an update. */
#define WDT_SECONDS 30

static void watchdogBegin() {
  NRF_WDT->CONFIG = (WDT_CONFIG_HALT_Pause << WDT_CONFIG_HALT_Pos) |
                    (WDT_CONFIG_SLEEP_Run  << WDT_CONFIG_SLEEP_Pos);
  NRF_WDT->CRV = (32768UL * WDT_SECONDS) - 1;      // 32.768 kHz clock
  NRF_WDT->RREN = (WDT_RREN_RR0_Enabled << WDT_RREN_RR0_Pos);
  NRF_WDT->TASKS_START = 1;
}
static inline void watchdogFeed() { NRF_WDT->RR[0] = WDT_RR_RR_Reload; }

void setup() {
  Serial.begin(115200);

  pinMode(LED_RED, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE, OUTPUT);
  setLed(false, false, false);

  PDM.onReceive(onPDMdata);
  if (!PDM.begin(1, PDM_RATE)) {
    Serial.println("ERR: PDM.begin failed");
    while (1) { delay(100); }
  }
  PDM.setGain(currentGain);    // must follow begin() -- see applyGain()

  // Must precede begin(): raises the SoftDevice event length and queue depth,
  // without which sustained notify throughput collapses.
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin(1, 0);
  Bluefruit.setTxPower(txPower);
  Bluefruit.setName("XIAO-MIC");
  Bluefruit.autoConnLed(false);   // we drive the RGB LED ourselves
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
  infoChar.setMaxLen(40);
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

  updateLed();
  watchdogBegin();
  Serial.println("XIAO-MIC advertising");

  // Last, deliberately: the radio must come up even if the IMU misbehaves.
  InternalFS.begin();
  {
    Settings cfg;
    if (settingsLoad(&cfg)) {
      currentGain  = cfg.gain;
      use16k       = cfg.use16k;
      vadEnabled   = cfg.vadEnabled;
      vadThresh    = cfg.vadThresh;
      ledLevel     = cfg.ledLevel;
      ledMode      = cfg.ledMode;
      backlogMode  = cfg.backlogMode;
      micPowerSave = cfg.micPowerSave;
      tapEnabled   = cfg.tapEnabled;
      txPower      = cfg.txPower;
      Serial.println("settings: restored");
    } else {
      Serial.println("settings: defaults");
    }
  }

  pinMode(VBAT_ENABLE, OUTPUT);
  digitalWrite(VBAT_ENABLE, HIGH);
  pinMode(PIN_CHG, INPUT);
  analogReference(AR_INTERNAL_3_0);
  analogReadResolution(12);
  {
    Settings cfg;
    setFastCharge(settingsLoad(&cfg) ? cfg.fastCharge : false);
  }
  batteryMv = readBatteryMv();

  qspiOk = qspiBegin();
  Serial.println(qspiOk ? "QSPI: store-and-forward ready" : "QSPI: not found");

  imuReady = imuTapBegin();
  publishInfo();               // republish now that IMU status is known
  Serial.println(imuReady ? "IMU: double-tap armed" : "IMU: not found");
}

// ---------------------------------------------------------------- loop

void loop() {
  servicePulse();

  // Sampling the battery is slow and it moves slowly; once every few seconds
  // is plenty and keeps the ADC off the rest of the time.
  static uint32_t lastBatMs = 0;
  if (millis() - lastBatMs > 5000) {
    lastBatMs = millis();
    batteryMv = readBatteryMv();
    charging = digitalRead(PIN_CHG) == LOW;   // ~CHG is active low
  }

  static uint32_t lastInfoMs = 0;
  if (millis() - lastInfoMs > 500) { lastInfoMs = millis(); publishInfo(); }

  // Double-tap toggles capture. Debounced in software on top of the sensor's
  // own timing windows, since one physical tap pair can latch more than once.
  if (imuReady && tapEnabled && imuDoubleTap()) {
    uint32_t now = millis();
    if (now - lastTapMs > 600) {
      lastTapMs = now;
      streaming = !streaming;          // arm/disarm, connected or not
      ringTail = ringHead;
      vadHangover = 0;
      preRollCount = 0;
      applyConnInterval();
      settingsTouch();
      updateLed();
    }
  }

  if (!streaming) {
    ringTail = ringHead;      // stay live rather than accumulating stale audio
    // The PDM mic is around 1 mA, which is meaningful against a budget near
    // 8 mA. Nothing is being recorded while disarmed, so stop it entirely.
    if (micPowerSave && micRunning) {
      PDM.end();
      digitalWrite(PIN_PDM_PWR, LOW);
      micRunning = false;
    }
    delay(20);
    return;
  }

  if (!micRunning) {
    digitalWrite(PIN_PDM_PWR, HIGH);
    delay(5);
    PDM.begin(1, PDM_RATE);
    PDM.setGain(currentGain);        // begin() resets gain -- see applyGain()
    micRunning = true;
    ringTail = ringHead;
    delay(5);
    return;
  }

  const bool haveBacklog = connected && qspiOk && (qspiPendingBytes() || drainLen);

  // Mode 0: finish the backlog first, so the host receives the conversation in
  // order rather than with a hole in the middle. Live audio waits.
  if (haveBacklog && backlogMode == 0) {
    // Live audio is deliberately discarded while catching up, so keep the
    // ring drained rather than letting it overflow. Otherwise the overrun
    // counter fills with events that are expected and harmless, and the one
    // number that would reveal real dropped audio becomes meaningless.
    ringTail = ringHead;
    drainBacklog();
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

  emitFrame(frameSamples, outSamples, seq, voiced);
  seq++;

  // Mode 1: one recovered frame per live frame, so the backlog empties at
  // roughly realtime without ever starving the live stream.
  if (haveBacklog && backlogMode == 1) drainOne();
}
