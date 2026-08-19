/*
 * Boswell — Zephyr firmware.
 *
 * Same job as the Arduino build and the same wire protocol, restructured
 * around threads: capture runs on its own thread and blocks on the DMIC
 * driver, so there is no polling loop deciding how often to look at anything.
 * That polling was the source of two separate faults in the Arduino version.
 */

#include "proto.h"
#include "codec.h"
#include "mic.h"
#include "ble_audio.h"
#include "imu_tap.h"
#include "battery.h"
#include "led.h"
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/hci_vs.h>
#include "cfg_store.h"
#include "qspi_store.h"

#include <stdlib.h>
#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/watchdog.h>
#include <zephyr/usb/usb_device.h>
#include <zephyr/sys/reboot.h>
#include <hal/nrf_power.h>
#include <zephyr/shell/shell.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

struct boswell_state g_state = {
    .streaming    = false,
    .use16k       = false,       /* 8 kHz fits a Bluetooth 4.0 host */
    .vad_enabled  = false,
    .vad_thresh   = 1120,
    .gain         = 50,
    .led_level    = 255,
    .led_mode     = 1,           /* blink: ~1% of the power of staying lit */
    /* On by default. The device is meant to be worn out of range of its
     * host, and buffering is the difference between losing that stretch of
     * conversation and paying for it in latency. */
    .backlog_mode = 1,
    .mic_power_save = 1,
    .tx_power     = 4,
};

/* ------------------------------------------------------------------- usb */

/* USB is only brought up when the cable is actually supplying power.
 *
 * Enabling the USB device keeps the 32 MHz crystal running whether or not
 * anything is attached, which is wasted current on a device meant to be worn
 * off a small cell. VBUS is polled rather than assumed so that plugging in
 * later still gets a console: once enabled it is left enabled, because
 * cycling the USB stack at runtime is a good deal more fragile than leaving
 * a peripheral powered while the cable that powers it is attached anyway. */
static bool usb_up;

static bool vbus_present(void)
{
    return (nrf_power_usbregstatus_get(NRF_POWER) &
            NRF_POWER_USBREGSTATUS_VBUSDETECT_MASK) != 0;
}

static void usb_service(void)
{
    if (usb_up || !vbus_present()) {
        return;
    }
    if (usb_enable(NULL) == 0) {
        usb_up = true;
        LOG_INF("USB attached");
    }
}

/* ---------------------------------------------------------------- leds */

/* blue advertising · green capturing · red connected but idle · magenta
 * draining the flash backlog. Brightness and steady-vs-pulse live in led.c. */
static void led_state(void)
{
    /* The wearer's first question is "is it recording?", so that decides the
     * colour: green while capturing, red while stopped. Everything else is
     * secondary and must not be able to mask it.
     *
     * A real backlog replay still shows magenta, but only a real one. The
     * test used to be "any pending byte at all", which is briefly true during
     * ordinary live streaming as frames pass through the staging ring, so the
     * light sat magenta nearly all the time. A quarter of a second of audio
     * is the smallest backlog worth reporting.
     *
     * Being armed with no host used to come out as red plus blue, which is
     * the same magenta as draining -- two unrelated states, one colour. It is
     * green now, because the device is recording either way; where the audio
     * is going is what the blue channel is for. */
    if (qspi_store_pending() > 2048 && ble_audio_ready()) {
        led_set_colour(true, false, true);          /* magenta: replaying */
    } else if (g_state.streaming) {
        led_set_colour(false, true, false);         /* green: capturing */
    } else if (!ble_audio_connected()) {
        led_set_colour(false, false, true);         /* blue: waiting for a host */
    } else {
        led_set_colour(true, false, false);         /* red: connected, stopped */
    }
}

/* ---------------------------------------------------------------- watchdog */

static const struct device *wdt_dev;
static int wdt_ch = -1;

static void watchdog_init(void)
{
    wdt_dev = DEVICE_DT_GET(DT_NODELABEL(wdt0));
    if (!device_is_ready(wdt_dev)) {
        LOG_WRN("watchdog unavailable");
        return;
    }
    struct wdt_timeout_cfg cfg = {
        .window = { .min = 0, .max = 30000 },   /* generous: a flash erase must not trip it */
        .callback = NULL,
        .flags = WDT_FLAG_RESET_SOC,
    };
    wdt_ch = wdt_install_timeout(wdt_dev, &cfg);
    if (wdt_ch < 0 || wdt_setup(wdt_dev, WDT_OPT_PAUSE_HALTED_BY_DBG)) {
        LOG_WRN("watchdog setup failed");
        wdt_ch = -1;
    }
}

static inline void watchdog_feed(void)
{
    if (wdt_ch >= 0) {
        wdt_feed(wdt_dev, wdt_ch);
    }
}

/* ---------------------------------------------------------------- dfu

 * Zephyr does not implement the 1200-baud touch the Arduino core used, so
 * without this every firmware update needs someone physically double-tapping
 * RESET. The Adafruit bootloader checks GPREGRET on boot: 0x57 keeps it in
 * UF2 mode instead of jumping to the application. Setting that and resetting
 * makes flashing scriptable again.
 */
#define DFU_MAGIC_UF2 0x57      /* mass-storage / serial DFU */
#define DFU_MAGIC_OTA 0xA8      /* the bootloader's own BLE DFU */

static void reboot_to_bootloader(uint8_t magic)
{
    LOG_INF("rebooting into the bootloader (magic 0x%02x)", magic);
    nrf_power_gpregret_set(NRF_POWER, 0, magic);
    k_sleep(K_MSEC(100));
    sys_reboot(SYS_REBOOT_COLD);
}

/* Shell commands, so the board can be driven without Bluetooth. */
static int cmd_dfu(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    shell_print(sh, "rebooting into the bootloader");
    reboot_to_bootloader(DFU_MAGIC_UF2);
    return 0;
}

/* Reboots into the bootloader's Bluetooth DFU mode instead of mass storage,
 * which is what makes updating without a cable possible at all. */
static int cmd_ota(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    shell_print(sh, "rebooting into BLE DFU");
    reboot_to_bootloader(DFU_MAGIC_OTA);
    return 0;
}

static int cmd_imu(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    int err = imu_tap_reprobe();
    uint8_t p[4];
    imu_tap_probe(p);
    uint32_t n[4];
    imu_tap_counters(n);
    shell_print(sh, "imu present=%d addr=0x%02x probe=%02x %02x %02x %02x (init %d)",
                imu_tap_present(), imu_tap_addr(), p[0], p[1], p[2], p[3], err);
    shell_print(sh, "taps: irq=%u double=%u accepted=%u debounced=%u",
                n[0], n[1], n[2], n[3]);
    return 0;
}

static int cmd_debounce(const struct shell *sh, size_t argc, char **argv)
{
    if (argc < 2) {
        shell_print(sh, "debounce = %u ms", imu_tap_get_debounce());
        return 0;
    }
    imu_tap_set_debounce((uint32_t)atoi(argv[1]));
    cfg_store_touch();
    shell_print(sh, "debounce -> %u ms", imu_tap_get_debounce());
    return 0;
}

static int cmd_taps(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    uint32_t n[4];
    imu_tap_counters(n);
    shell_print(sh, "irq=%u double=%u accepted=%u debounced=%u",
                n[0], n[1], n[2], n[3]);
    return 0;
}

static int cmd_tap(const struct shell *sh, size_t argc, char **argv)
{
    if (argc < 2) {
        shell_print(sh, "usage: boswell tap <0-31>   (lower = more sensitive)");
        return 0;
    }
    uint8_t t = (uint8_t)atoi(argv[1]);
    imu_tap_set_threshold(t);
    cfg_store_touch();
    shell_print(sh, "tap threshold -> %u", t & 0x1F);
    return 0;
}

static int cmd_stream(const struct shell *sh, size_t argc, char **argv)
{
    if (argc < 2) {
        shell_print(sh, "streaming=%d", g_state.streaming);
        return 0;
    }
    g_state.streaming = (argv[1][0] == '1' || argv[1][0] == 'o' ? 1 : 0);
    if (argv[1][0] == 'o' && argv[1][1] == 'f') {
        g_state.streaming = 0;
    }
    ble_audio_apply_conn_params(g_state.streaming);
    ble_audio_publish_info();
    led_state();
    shell_print(sh, "streaming=%d", g_state.streaming);
    return 0;
}

static int cmd_reboot(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    shell_print(sh, "rebooting");
    k_sleep(K_MSEC(100));
    sys_reboot(SYS_REBOOT_COLD);
    return 0;
}

static int cmd_status(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    shell_print(sh, "connected=%d streaming=%d gain=%u rate=%s mic=%d",
                ble_audio_connected(), g_state.streaming, g_state.gain,
                g_state.use16k ? "16k" : "8k", mic_running());
    battery_sample();
    uint32_t qs[4];
    qspi_store_stats(qs);
    shell_print(sh, "qspi ready=%d pending=%u B dropped=%u cap=%u KB",
                qspi_store_ready(), qspi_store_pending(),
                qspi_store_dropped(), qspi_store_capacity() / 1024);
    shell_print(sh, "qspi pushes=%u pages=%u erases=%u wake=%u",
                qs[0], qs[1], qs[2], qs[3]);
    shell_print(sh, "cfg store=%d  tap_thresh=%u debounce=%u ms",
                cfg_store_ready(), imu_tap_get_threshold(),
                imu_tap_get_debounce());
    shell_print(sh, "battery=%u mV (%u%%) charging=%d  imu=%d",
                battery_mv(), battery_percent(), battery_charging(),
                imu_tap_present());
    return 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(boswell_cmds,
    SHELL_CMD(dfu, NULL, "Reboot into the bootloader for flashing", cmd_dfu),
    SHELL_CMD(ota, NULL, "Reboot into the bootloader's BLE DFU mode", cmd_ota),
    SHELL_CMD(status, NULL, "Show capture state", cmd_status),
    SHELL_CMD(reboot, NULL, "Restart the firmware", cmd_reboot),
    SHELL_CMD(stream, NULL, "Arm/disarm capture (on|off)", cmd_stream),
    SHELL_CMD(imu, NULL, "Re-probe the IMU and report", cmd_imu),
    SHELL_CMD(tap, NULL, "Set double-tap threshold (0-31)", cmd_tap),
    SHELL_CMD(taps, NULL, "Show tap counters", cmd_taps),
    SHELL_CMD(debounce, NULL, "Get/set tap debounce in ms", cmd_debounce),
    SHELL_SUBCMD_SET_END
);
SHELL_CMD_REGISTER(boswell, &boswell_cmds, "Boswell commands", NULL);

/* Radio transmit power.
 *
 * Lower power costs range and saves current, which is a real trade on a
 * device worn all day; the host exposes it because the right answer depends
 * on how far the wearer is from the machine. Set through the controller's
 * vendor-specific HCI command, since there is no portable API for it. */
static void set_tx_power(int8_t dbm)
{
    struct bt_hci_cp_vs_write_tx_power_level *cp;
    struct net_buf *buf;

    buf = bt_hci_cmd_create(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, sizeof(*cp));
    if (!buf) {
        LOG_WRN("no buffer for tx power");
        return;
    }
    cp = net_buf_add(buf, sizeof(*cp));
    cp->handle = 0;
    cp->handle_type = BT_HCI_VS_LL_HANDLE_TYPE_ADV;
    cp->tx_power_level = dbm;

    int err = bt_hci_cmd_send_sync(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, buf, NULL);
    if (err) {
        LOG_WRN("tx power %d dBm rejected (%d)", dbm, err);
        return;
    }
    g_state.tx_power = dbm;
    LOG_INF("tx power %d dBm", dbm);
}

/* ---------------------------------------------------------------- control */

static void on_ctrl(uint8_t op, uint8_t arg)
{
    switch (op) {
    case CTRL_STREAM:
        g_state.streaming = arg != 0;
        ble_audio_apply_conn_params(g_state.streaming);
        break;
    case CTRL_RATE:         g_state.use16k = arg != 0;    break;
    case CTRL_GAIN:         g_state.gain = arg; mic_set_gain(arg); break;
    case CTRL_VAD:          g_state.vad_enabled = arg != 0; break;
    case CTRL_VAD_THRESH:   g_state.vad_thresh = arg * 32; break;
    case CTRL_BACKLOG_MODE: g_state.backlog_mode = arg ? 1 : 0; break;
    case CTRL_TAP_ENABLE:   imu_tap_set_enabled(arg != 0); break;
    case CTRL_TAP_THRESH:   imu_tap_set_threshold(arg);    break;
    case CTRL_CLEAR_BUFFER: qspi_store_reset();            break;
    case CTRL_FAST_CHARGE:  battery_set_fast_charge(arg != 0); break;
    case CTRL_TX_POWER:     set_tx_power((int8_t)arg);     break;
    case CTRL_LED_LEVEL:
        g_state.led_level = arg;
        led_set_level(arg);
        break;
    case CTRL_LED_MODE:
        g_state.led_mode = arg ? 1 : 0;
        led_set_mode(g_state.led_mode);
        break;
    case CTRL_MIC_SAVE:     g_state.mic_power_save = arg ? 1 : 0; break;
    case CTRL_DFU:
        /* Deliberately awkward arguments, not a stray write. */
        if (arg == 0x5A) {
            reboot_to_bootloader(DFU_MAGIC_UF2);
        } else if (arg == 0xA5) {
            reboot_to_bootloader(DFU_MAGIC_OTA);
        }
        break;
    default: break;
    }
    ble_audio_publish_info();
    led_state();
    cfg_store_touch();
}

/* Hands one replayed record to the link, from the writer thread.
 *
 * The frame is stamped as coming from flash on the way out. Replayed frames
 * carry the sequence numbers they were captured with, so a host that cannot
 * tell them from live audio sees the sequence jump backwards and reports
 * nonsense packet loss. Flags are not part of the ADPCM state, so setting
 * the bit here cannot affect decoding. */
static bool drain_to_host(const uint8_t *rec, uint16_t len)
{
    if (len <= 2 || !ble_audio_ready()) {
        return false;
    }
    uint8_t stamped[MAX_FRAME_LEN];
    if (len > sizeof(stamped)) {
        return false;
    }
    memcpy(stamped, rec, len);
    stamped[2] |= FLAG_FROM_FLASH;
    return ble_audio_send(stamped, len) == 0;
}

/* A double tap toggles capture: the device is worn, so the only control that
 * works without looking at it is touch. Green means capturing, red means
 * stopped, and the LED is the only feedback the wearer gets. */
static void on_double_tap(void)
{
    g_state.streaming = !g_state.streaming;
    LOG_INF("double tap -> %s", g_state.streaming ? "capturing" : "stopped");
    ble_audio_apply_conn_params(g_state.streaming);
    ble_audio_publish_info();
    led_state();
}

/* ---------------------------------------------------------- settings glue */

static void settings_snapshot(struct boswell_settings *s)
{
    memset(s, 0, sizeof(*s));
    s->gain            = g_state.gain;
    s->use16k          = g_state.use16k;
    s->vad_enabled     = g_state.vad_enabled;
    s->vad_thresh      = g_state.vad_thresh;
    s->led_level       = g_state.led_level;
    s->led_mode        = g_state.led_mode;
    s->backlog_mode    = g_state.backlog_mode;
    s->mic_power_save  = g_state.mic_power_save;
    s->tap_thresh      = imu_tap_get_threshold();
    s->tap_debounce_ms = (uint16_t)imu_tap_get_debounce();
    s->tx_power        = g_state.tx_power;
}

static void settings_apply(const struct boswell_settings *s)
{
    g_state.gain           = s->gain;
    g_state.use16k         = s->use16k;
    g_state.vad_enabled    = s->vad_enabled;
    g_state.vad_thresh     = s->vad_thresh;
    g_state.led_level      = s->led_level;
    g_state.led_mode       = s->led_mode;
    g_state.backlog_mode   = s->backlog_mode;
    g_state.mic_power_save = s->mic_power_save;
    g_state.tx_power       = s->tx_power;
    if (s->tap_thresh) {
        imu_tap_set_threshold(s->tap_thresh);
    }
    if (s->tap_debounce_ms) {
        imu_tap_set_debounce(s->tap_debounce_ms);
    }
}

/* ---------------------------------------------------------------- capture */

#define CAPTURE_STACK 4096
K_THREAD_STACK_DEFINE(capture_stack, CAPTURE_STACK);
static struct k_thread capture_thread;

static int16_t raw[MAX_SAMPLES];
static int16_t frame[MAX_SAMPLES];
static uint8_t wire[MAX_FRAME_LEN];

static void capture_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    uint16_t seq = 0;
    int hangover = 0;

    while (1) {
        watchdog_feed();

        if (!g_state.streaming) {
            if (g_state.mic_power_save && mic_running()) {
                mic_stop();       /* the microphone is ~1 mA doing nothing */
            }
            k_sleep(K_MSEC(50));
            continue;
        }
        if (!mic_running()) {
            if (mic_start() != 0) {
                k_sleep(K_MSEC(200));
                continue;
            }
            mic_set_gain(g_state.gain);   /* configuration resets it */
        }

        int got = mic_read_frame(raw, MAX_SAMPLES, K_MSEC(200));
        if (got <= 0) {
            /* Must sleep, not just continue. dmic_read can fail immediately,
             * and a bare continue here spins without ever yielding. */
            k_sleep(K_MSEC(5));
            continue;
        }

        int count = got;
        if (!g_state.use16k) {
            /* 2:1 decimation by pair averaging: a 2-tap filter whose null sits
             * at 4 kHz, which is where the fold lands. */
            count = got / 2;
            for (int i = 0; i < count; i++) {
                frame[i] = (int16_t)(((int32_t)raw[2 * i] + raw[2 * i + 1]) / 2);
            }
        } else {
            memcpy(frame, raw, got * sizeof(int16_t));
        }

        bool voiced = true;
        if (g_state.vad_enabled) {
            uint32_t rms = codec_rms(frame, count);
            if (rms >= g_state.vad_thresh) {
                hangover = 15;              /* 300 ms release */
            } else if (hangover > 0) {
                hangover--;
            }
            voiced = hangover > 0;
            if (!voiced) {
                seq++;                      /* the gap is intentional, not loss */
                continue;
            }
        }

        uint8_t flags = 0;
        if (g_state.use16k)      flags |= FLAG_16K;
        if (voiced)              flags |= FLAG_VOICED;
        if (g_state.vad_enabled) flags |= FLAG_VAD_ON;

        uint16_t len = codec_build_frame(frame, count, seq,
                                         k_uptime_get_32(), flags, wire);

        if (!ble_audio_ready()) {
            /* Nobody to send to. Buffer rather than discard: the point of the
             * flash is that walking out of range costs latency, not audio. */
            if (g_state.backlog_mode && qspi_store_ready()) {
                qspi_store_push(wire, (uint8_t)len);
            }
            seq++;
            continue;
        }

        /* With a backlog outstanding, the frame just captured joins the back
         * of the queue rather than going straight out: interleaving live
         * audio with replayed audio splices two different moments of the
         * conversation together. The writer thread replays the queue in
         * order. Strict ordering costs latency, which is the trade the flash
         * buffer exists to make. */
        if (qspi_store_pending() > 0 && g_state.backlog_mode &&
            qspi_store_ready()) {
            qspi_store_push(wire, (uint8_t)len);
            seq++;
            continue;
        }

        if (ble_audio_send(wire, len) == 0) {
            seq++;
        }
    }
}

/* ---------------------------------------------------------------- main */

int main(void)
{
    /* USB and the LED come first so that whatever happens next can be seen.
     * The first version of this initialised the microphone and Bluetooth
     * before either, and when it faulted there was no way to tell why. */
    /* Only wait for enumeration when there is a host to enumerate with.
     * On battery this saves a second and a half of boot spent on nothing. */
    if (vbus_present()) {
        (void)usb_enable(NULL);
        usb_up = true;
        k_sleep(K_MSEC(1500));
    }

    (void)led_init();
    led_set_level(g_state.led_level);
    led_set_mode(g_state.led_mode);
    led_set_colour(false, false, true);
    LOG_INF("Boswell starting");
    LOG_INF("shell ready: 'boswell dfu' reboots for flashing");

    int err = mic_init();
    LOG_INF("mic_init -> %d", err);

    err = ble_audio_init(on_ctrl);
    LOG_INF("ble_audio_init -> %d", err);

    qspi_store_set_drain(drain_to_host, ble_audio_ready);
    err = cfg_store_init();
    LOG_INF("cfg_store_init -> %d", err);

    err = qspi_store_init();
    LOG_INF("qspi_store_init -> %d", err);

    err = battery_init();
    LOG_INF("battery_init -> %d", err);

    err = imu_tap_init(on_double_tap);
    LOG_INF("imu_tap_init -> %d", err);

    /* After the drivers exist, so applying a restored value reaches hardware
     * rather than only updating a variable that init then overwrites. */
    struct boswell_settings saved;
    if (cfg_store_load(&saved)) {
        settings_apply(&saved);
        mic_set_gain(g_state.gain);
        led_set_level(g_state.led_level);
        led_set_mode(g_state.led_mode);
    }

    watchdog_init();
    LOG_INF("watchdog ready");

    k_thread_create(&capture_thread, capture_stack, CAPTURE_STACK,
                    capture_fn, NULL, NULL, NULL,
                    /* Preemptible, and deliberately below the Bluetooth RX
                     * thread. As a cooperative thread it outranked the host
                     * stack and could only be descheduled by blocking. */
                    K_PRIO_PREEMPT(10), 0, K_NO_WAIT);
    k_thread_name_set(&capture_thread, "capture");

    /* Ticks at the pulse resolution rather than the housekeeping interval: a
     * 25 ms flash cannot be driven by a loop that wakes twice a second. */
    int64_t next_house = 0, next_batt = 0;
    while (1) {
        int64_t now = k_uptime_get();

        led_service();
        usb_service();

        if (now >= next_house) {
            next_house = now + 500;
            watchdog_feed();
            led_state();
        }
        if (now >= next_batt) {
            next_batt = now + 30000;
            battery_sample();
        }
        struct boswell_settings cur;
        settings_snapshot(&cur);
        cfg_store_service(&cur);
        k_sleep(K_MSEC(10));
    }
    return 0;
}
