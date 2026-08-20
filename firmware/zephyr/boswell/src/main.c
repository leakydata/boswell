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
#include <zephyr/sys/byteorder.h>
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

/* ---------------------------------------------------------------- reset */

static uint32_t last_reset_reason;

static void report_reset_reason(void)
{
    uint32_t r = nrf_power_resetreas_get(NRF_POWER);
    nrf_power_resetreas_clear(NRF_POWER, r);   /* or it accumulates forever */
    last_reset_reason = r;

    if (r == 0) {
        LOG_INF("reset: power-on");
    } else {
        LOG_INF("reset: 0x%08x%s%s%s%s%s", r,
                (r & NRF_POWER_RESETREAS_DOG_MASK)      ? " watchdog"  : "",
                (r & NRF_POWER_RESETREAS_RESETPIN_MASK) ? " pin"       : "",
                (r & NRF_POWER_RESETREAS_SREQ_MASK)     ? " soft"      : "",
                (r & NRF_POWER_RESETREAS_LOCKUP_MASK)   ? " lockup"    : "",
                (r & NRF_POWER_RESETREAS_OFF_MASK)      ? " wake"      : "");
    }
}

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
static bool led_probing;

static void led_state(void)
{
    if (led_probing) {
        return;
    }
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

/* The watchdog is only fed once every thread that matters has said it is
 * alive.
 *
 * It used to be fed from the housekeeping loop and from the capture thread
 * independently, which meant either one alone kept it quiet: capture could
 * wedge on the microphone, or the flash writer could deadlock, and the
 * housekeeping loop would go on feeding a watchdog that was no longer
 * protecting anything. A watchdog that reports health it has not checked is
 * worse than none, because it is believed.
 *
 * Bluetooth and the IMU are deliberately not watched. A stalled BLE thread
 * costs the link, which the never-subscribed guard and re-advertising already
 * recover from, and the flash keeps the audio meanwhile; a stalled IMU costs
 * step counts. Neither loses a recording. Resetting the board for them would
 * trade a recoverable fault for a guaranteed gap in the audio, which is the
 * one thing this device exists not to do.
 */
#define WDT_MAIN    BIT(0)
#define WDT_CAPTURE BIT(1)
#define WDT_QSPI    BIT(2)
#define WDT_TX      BIT(3)
#define WDT_ALL     (WDT_MAIN | WDT_CAPTURE | WDT_QSPI | WDT_TX)

static atomic_t wdt_seen;
/* Frames the radio refused after every retry. */
static uint32_t notify_drops;
/* Motion reads the sensor refused, and frames abandoned because none
 * succeeded. A count is the difference between a quiet sensor and a dead one. */
static uint32_t imu_read_fails, imu_empty_frames;
/* Which threads exist. Requiring a check-in from a thread that was never
 * created is a reboot loop with no way out: if the flash fails to probe, the
 * writer thread is never started, WDT_QSPI is never set, and the watchdog
 * resets the board every thirty seconds forever -- on a device that would
 * otherwise have run fine without its backlog. The mask is built from what
 * actually came up. */
static atomic_t wdt_required = ATOMIC_INIT(WDT_MAIN | WDT_CAPTURE | WDT_TX);

void watchdog_expect(uint32_t who)
{
    atomic_or(&wdt_required, who);
}

void watchdog_checkin(uint32_t who)
{
    atomic_or(&wdt_seen, who);
}

static void watchdog_service(void)
{
    if (wdt_ch < 0) {
        return;
    }
    uint32_t need = (uint32_t)atomic_get(&wdt_required);
    if (((uint32_t)atomic_get(&wdt_seen) & need) != need) {
        return;                  /* somebody has not reported; let it bite */
    }
    atomic_clear(&wdt_seen);
    wdt_feed(wdt_dev, wdt_ch);
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

static int cmd_led(const struct shell *sh, size_t argc, char **argv)
{
    if (argc < 2) {
        shell_print(sh, "usage: boswell led <rgb> | probe   (100 = red, 000 = off)");
        shell_print(sh, "ready=%d level=%u mode=%u", led_ready(),
                    g_state.led_level, g_state.led_mode);
        return 0;
    }
    if (argv[1][0] == 'p') {          /* 'probe': the shell eats '?' */
        /* Sweep every state and report what the pins actually do. */
        static const struct { bool r, g, b; const char *name; } probe[] = {
            { false, false, false, "off   " },
            { true,  false, false, "red   " },
            { false, true,  false, "green " },
            { false, false, true,  "blue  " },
        };
        led_probing = true;      /* keep led_state() out of the measurement */
        for (int i = 0; i < 4; i++) {
            led_force(probe[i].r, probe[i].g, probe[i].b);
            k_sleep(K_MSEC(120));
            uint8_t lv[3];
            led_pin_levels(lv);
            shell_print(sh, "  %s asked r=%d g=%d b=%d -> pins r=%d g=%d b=%d  (0 = lit)",
                        probe[i].name, probe[i].r, probe[i].g, probe[i].b,
                        lv[0], lv[1], lv[2]);
        }
        led_probing = false;
        led_set_mode(g_state.led_mode);
        led_state();
        return 0;
    }
    const char *v = argv[1];
    bool r = v[0] == '1', g = v[1] && v[1] == '1', b = v[1] && v[2] == '1';
    led_force(r, g, b);
    shell_print(sh, "forced r=%d g=%d b=%d", r, g, b);
    return 0;
}

static int cmd_adv(const struct shell *sh, size_t argc, char **argv)
{
    ARG_UNUSED(argc); ARG_UNUSED(argv);
    int err = ble_audio_advertise_now();
    shell_print(sh, "advertise -> %d (advertising=%d)", err,
                ble_audio_advertising());
    return 0;
}

static int cmd_steps(const struct shell *sh, size_t argc, char **argv)
{
    if (argc > 1 && argv[1][0] == 'r') {
        imu_steps_reset();
        shell_print(sh, "step counter reset");
        return 0;
    }
    imu_motion_poll();
    shell_print(sh, "steps=%u", imu_steps());
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
    /* link and subscribed are reported apart. A host can hold a connection
     * without ever enabling notifications, which is a normal transient state
     * and looks identical to a dead radio if the two are merged. */
    shell_print(sh, "link=%d subscribed=%d streaming=%d gain=%u rate=%s mic=%d",
                ble_audio_linked(), ble_audio_connected(), g_state.streaming,
                g_state.gain, g_state.use16k ? "16k" : "8k", mic_running());
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
    shell_print(sh, "advertising=%d  (unreachable = no link and no advertising)",
                ble_audio_advertising());
    uint32_t idle[3];
    ble_audio_idle_stats(idle);
    shell_print(sh, "notify drops=%u", notify_drops);
    { uint32_t ss[4]; ble_audio_send_stats(ss); shell_print(sh, "send calls=%u retries=%u avg=%u us max=%u us", ss[0], ss[1], ss[3], ss[2]); }
    shell_print(sh, "imu read fails=%u empty frames=%u",
                imu_read_fails, imu_empty_frames);
    { uint32_t ws[2]; int we = 0; qspi_store_write_stats(ws, &we);
      shell_print(sh, "qspi write fails=%u erase fails=%u err=%d",
                  ws[0], ws[1], we); }
    { uint32_t ps[8]; int le = 0; qspi_store_pop_stats(ps, &le);
      shell_print(sh, "qspi peeks=%u refused=%u skipped=%u unstuck=%u fails=%u scanned=%u err=%d",
                  ps[4], ps[5], ps[6], ps[7], ps[0], ps[3], le); }
    { uint32_t dl[3]; ble_audio_dead_link_stats(dl);
      shell_print(sh, "dead-link fails=%u drops=%u silent=%u s", dl[0], dl[1], dl[2]); }
    shell_print(sh, "idle-guard armed=%u fired=%u dropped=%u",
                idle[0], idle[1], idle[2]);
    shell_print(sh, "last reset=0x%08x%s%s%s", last_reset_reason,
                (last_reset_reason & NRF_POWER_RESETREAS_DOG_MASK)    ? " watchdog" : "",
                (last_reset_reason & NRF_POWER_RESETREAS_LOCKUP_MASK) ? " lockup"   : "",
                last_reset_reason == 0 ? " power-on" : "");
    shell_print(sh, "imu_stream=%u Hz gyro=%d", g_state.imu_hz,
                imu_gyro_enabled());
    shell_print(sh, "steps=%u tilt=%d motion=%d tap=%d ctrl10=0x%02x",
                imu_steps(), imu_tilt_peek(), imu_significant_motion_peek(),
                imu_tap_enabled(), imu_motion_config());
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
    SHELL_CMD(steps, NULL, "Show step count, or 'steps reset'", cmd_steps),
    SHELL_CMD(adv, NULL, "Force advertising to restart", cmd_adv),
    SHELL_CMD(led, NULL, "Force LED channels, e.g. 'led 100'", cmd_led),
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
/* One handle. The controller takes advertising and connections separately. */
static int tx_power_one(uint8_t handle_type, uint16_t handle, int8_t dbm,
                        int8_t *accepted)
{
    struct bt_hci_cp_vs_write_tx_power_level *cp;
    struct net_buf *buf, *rsp = NULL;

    buf = bt_hci_cmd_create(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, sizeof(*cp));
    if (!buf) {
        return -ENOBUFS;
    }
    cp = net_buf_add(buf, sizeof(*cp));
    cp->handle = sys_cpu_to_le16(handle);
    cp->handle_type = handle_type;
    cp->tx_power_level = dbm;

    int err = bt_hci_cmd_send_sync(BT_HCI_OP_VS_WRITE_TX_POWER_LEVEL, buf, &rsp);

    if (err == 0 && rsp) {
        /* What the controller actually selected, which is not always what was
         * asked for -- it snaps to the levels the radio supports. Reporting
         * the request instead means the interface shows a number the radio is
         * not using. */
        struct bt_hci_rp_vs_write_tx_power_level *rp = (void *)rsp->data;

        if (accepted && rsp->len >= sizeof(*rp)) {
            *accepted = rp->selected_tx_power;
        }
    }
    if (rsp) {
        net_buf_unref(rsp);
    }
    return err;
}

/* Set the radio's transmit power.
 *
 * This wrote the advertising handle only, so the control the interface labels
 * "transmit power" changed how far the device could be discovered from and
 * left the connection carrying the audio exactly as it was. The connection is
 * the one that matters for range while recording.
 */
static void set_tx_power(int8_t dbm)
{
    int8_t accepted = dbm;
    int err = tx_power_one(BT_HCI_VS_LL_HANDLE_TYPE_ADV, 0, dbm, &accepted);

    if (err) {
        LOG_WRN("tx power %d dBm rejected for advertising (%d)", dbm, err);
        return;
    }

    int h = ble_audio_conn_handle();

    if (h >= 0) {
        int8_t conn_accepted = dbm;
        int cerr = tx_power_one(BT_HCI_VS_LL_HANDLE_TYPE_CONN, (uint16_t)h,
                                dbm, &conn_accepted);
        if (cerr) {
            LOG_WRN("tx power %d dBm rejected for the connection (%d)", dbm, cerr);
        } else {
            accepted = conn_accepted;
        }
    }

    g_state.tx_power = accepted;
    if (accepted != dbm) {
        LOG_INF("tx power %d dBm requested, %d dBm selected", dbm, accepted);
    } else {
        LOG_INF("tx power %d dBm", dbm);
    }
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
    case CTRL_IMU_STREAM:   g_state.imu_hz = arg;          break;
    case CTRL_IMU_GYRO:     imu_set_gyro(arg != 0);        break;
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

static void qspi_alive(void) { watchdog_checkin(WDT_QSPI); }

/* Hands one replayed record to the link, from the writer thread.
 *
 * The frame is stamped as coming from flash on the way out. Replayed frames
 * carry the sequence numbers they were captured with, so a host that cannot
 * tell them from live audio sees the sequence jump backwards and reports
 * nonsense packet loss. Flags are not part of the ADPCM state, so setting
 * the bit here cannot affect decoding. */
static int drain_to_host(const uint8_t *rec, uint16_t len)
{
    /* Too small or too large to be a frame. Not a transient condition, so
     * saying "try again" about it means trying again forever: the store
     * offers the same record, this refuses it, and the backlog never moves.
     * That is exactly what happened -- 2577 reads, 2577 refusals, and 163 KB
     * of audio that would never have been delivered. */
    if (len <= 2 || len > MAX_FRAME_LEN) {
        return -1;
    }
    if (!ble_audio_ready()) {
        return 0;                      /* nobody listening; keep it */
    }
    uint8_t stamped[MAX_FRAME_LEN];

    memcpy(stamped, rec, len);
    stamped[2] |= FLAG_FROM_FLASH;
    return ble_audio_send(stamped, len) == 0 ? 1 : 0;
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

/* Every field, clamped at the boundary where it comes back from storage.
 *
 * The control opcodes validate their arguments, and this path did not -- so a
 * value that could never arrive over Bluetooth could arrive from flash after
 * a corrupted write or a layout change. One of them is not merely untidy:
 * backlog_mode is published as (backlog_mode << 1) in info byte 5, and the
 * capture-state bit is 4, so a restored value of 2 makes the host read
 * "recording" on a device that is not. That is the failure this project keeps
 * finding in other forms.
 */
static bool tx_power_supported(int8_t dbm)
{
    static const int8_t ok[] = { -40, -20, -16, -12, -8, -4, 0, 3, 4, 8 };

    for (size_t i = 0; i < ARRAY_SIZE(ok); i++) {
        if (ok[i] == dbm) {
            return true;
        }
    }
    return false;
}

static void settings_apply(const struct boswell_settings *s)
{
    g_state.gain           = s->gain > 0x50 ? 0x50 : s->gain;
    g_state.use16k         = !!s->use16k;
    g_state.vad_enabled    = !!s->vad_enabled;
    /* Set over the wire as arg * 32 with arg a byte, so this is its ceiling. */
    g_state.vad_thresh     = s->vad_thresh > 255 * 32 ? 255 * 32 : s->vad_thresh;
    g_state.led_level      = s->led_level;          /* a byte is the range */
    g_state.led_mode       = s->led_mode ? 1 : 0;
    g_state.backlog_mode   = s->backlog_mode ? 1 : 0;
    g_state.mic_power_save = !!s->mic_power_save;
    g_state.tx_power       = tx_power_supported(s->tx_power) ? s->tx_power : 0;
    if (s->tap_thresh && s->tap_thresh <= 31) {     /* TAP_THS_6D is 5 bits */
        imu_tap_set_threshold(s->tap_thresh);
    }
    if (s->tap_debounce_ms >= 50 && s->tap_debounce_ms <= 5000) {
        imu_tap_set_debounce(s->tap_debounce_ms);
    }
}

/* ------------------------------------------------------------- imu stream */

/* Raw motion, sampled here and interpreted on the host.
 *
 * What counts as a step, or a gesture, or sitting down is a question that
 * will change, and changing it should not mean reflashing a device somebody
 * is wearing. The part's own pedometer stays on regardless: it keeps counting
 * while the device is out of range with nothing to stream to.
 */
#define IMU_STACK 1024
K_THREAD_STACK_DEFINE(imu_stack, IMU_STACK);
static struct k_thread imu_thread;

static void imu_stream_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    uint8_t  frame[IMU_HEADER_LEN + IMU_MAX_SAMPLES * 12];
    uint16_t seq = 0;

    for (;;) {
        uint8_t hz = g_state.imu_hz;
        if (hz == 0 || !ble_imu_ready()) {
            k_sleep(K_MSEC(200));
            continue;
        }
        bool gyro = imu_gyro_enabled();
        uint8_t stride = gyro ? 12 : 6;
        uint32_t period_us = 1000000u / hz;

        int n = 0;
        uint8_t *p = frame + IMU_HEADER_LEN;
        int64_t t0 = k_uptime_get();
        /* Bounded by attempts, not only by successes.
         *
         * This waited for IMU_MAX_SAMPLES good reads and nothing else, so a
         * sensor that had stopped answering left the thread here for as long
         * as the device stayed on: no frame, no error, and no watchdog either
         * because this thread does not check in. Motion simply stopped, and
         * the only way to find out was to notice the absence.
         *
         * Twice the samples wanted is enough slack for the odd failed read on
         * a shared bus and short enough that a dead sensor is reported within
         * one frame's time. */
        int attempts = 0;
        const int max_attempts = IMU_MAX_SAMPLES * 2;

        while (n < IMU_MAX_SAMPLES && attempts < max_attempts) {
            struct imu_sample s;

            attempts++;
            if (imu_read_motion(&s, gyro)) {
                const int16_t *src = &s.ax;
                for (int i = 0; i < (gyro ? 6 : 3); i++) {
                    *p++ = (uint8_t)(src[i] & 0xFF);
                    *p++ = (uint8_t)((src[i] >> 8) & 0xFF);
                }
                n++;
            } else {
                imu_read_fails++;
            }
            k_sleep(K_USEC(period_us));
        }
        if (n == 0) {
            /* Nothing to send, and saying so beats an empty frame that looks
             * like stillness. */
            if (imu_empty_frames++ % 50 == 0) {
                LOG_WRN("IMU returned no samples in %d attempts", attempts);
            }
            continue;
        }

        frame[0] = (uint8_t)(seq & 0xFF);
        frame[1] = (uint8_t)(seq >> 8);
        frame[2] = (gyro ? IMU_FLAG_GYRO : 0)
                 | (IMU_GYRO_FS_500 << IMU_GYRO_FS_SHIFT);
        frame[3] = (uint8_t)n;
        frame[4] = (uint8_t)(hz & 0xFF);
        frame[5] = (uint8_t)(hz >> 8);
        uint32_t t = (uint32_t)t0;
        frame[6] = (uint8_t)(t & 0xFF);
        frame[7] = (uint8_t)((t >> 8) & 0xFF);
        frame[8] = (uint8_t)((t >> 16) & 0xFF);
        frame[9] = (uint8_t)((t >> 24) & 0xFF);

        (void)ble_imu_send(frame, IMU_HEADER_LEN + n * stride);
        seq++;
    }
}

/* 16 kHz to 8 kHz, with an anti-alias filter that actually rejects.
 *
 * This was pair averaging, described in a comment as "a 2-tap filter whose
 * null sits at 4 kHz". It does not: a two-tap moving average has its null at
 * the sample rate over two, which is 8 kHz here, and only -3 dB at the 4 kHz
 * Nyquist the output is about to have. Everything from 4 to 8 kHz folded back
 * into the speech band at close to full amplitude -- sibilance landing on top
 * of vowels, which is exactly the content a transcriber needs.
 *
 * A 23-tap windowed-sinc at 3.4 kHz measures -18 dB at 4 kHz, -50 dB at 5 kHz
 * and -57 dB at 6 kHz, against -3 and -8 for the average. It costs about
 * 184k multiply-accumulates per second at 8 kHz output, which is nothing on
 * this part.
 *
 * The delay line persists across frames because the audio does; resetting it
 * per frame would put a discontinuity at every frame boundary.
 */
#define DEC_TAPS 23
static const int16_t dec_h[DEC_TAPS] = {
       64,    73,   -92,  -295,    41,   812,   482, -1538, -2217,  2188,
     9924, 13884,  9924,  2188, -2217, -1538,   482,   812,    41,  -295,
      -92,    73,    64,
};
static int16_t dec_hist[DEC_TAPS];

static int decimate_2to1(const int16_t *in, int n, int16_t *out)
{
    int produced = 0;

    for (int i = 0; i < n; i++) {
        memmove(&dec_hist[1], &dec_hist[0],
                (DEC_TAPS - 1) * sizeof(dec_hist[0]));
        dec_hist[0] = in[i];

        /* Every second input sample produces one output sample. */
        if ((i & 1) == 0) {
            continue;
        }
        int32_t acc = 0;
        for (int k = 0; k < DEC_TAPS; k++) {
            acc += (int32_t)dec_h[k] * dec_hist[k];
        }
        acc >>= 15;
        if (acc > 32767) {
            acc = 32767;
        } else if (acc < -32768) {
            acc = -32768;
        }
        out[produced++] = (int16_t)acc;
    }
    return produced;
}

/* ---------------------------------------------------------------- capture */

#define CAPTURE_STACK 4096
K_THREAD_STACK_DEFINE(capture_stack, CAPTURE_STACK);
static struct k_thread capture_thread;

/* ------------------------------------------------------- transmit thread */

/* Bluetooth notification is slow enough to starve the microphone.
 *
 * Measured on a 4.0 dongle with no Data Length Extension: a 172-byte frame
 * becomes seven 27-byte radio packets, and bt_gatt_notify() blocks inside the
 * stack for 88 ms on average and 197 ms at worst waiting for them to drain.
 * The capture thread called it directly, so it ran eleven times in fifteen
 * seconds instead of seven hundred and fifty; the PDM slab overran and every
 * subsequent read failed with a 200 ms timeout. Capture measured on its own,
 * writing to flash, ran at the full 58 frames a second -- the microphone was
 * never the problem.
 *
 * So the radio gets its own thread and a queue. The capture thread hands over
 * a frame and returns to the microphone immediately; when the queue fills,
 * frames spill to the flash backlog, which is what it is for. A slow link now
 * costs latency instead of audio.
 *
 * The queue holds about a second of audio, which is roughly the depth of the
 * PDM slab -- past that the link is not keeping up and flash is the right
 * place for the overflow. */
struct txframe {
    uint8_t len;
    uint8_t data[MAX_FRAME_LEN];
};

K_MSGQ_DEFINE(tx_q, sizeof(struct txframe), 48, 4);

static void tx_thread(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);

    for (;;) {
        struct txframe tf;

        if (k_msgq_get(&tx_q, &tf, K_MSEC(500)) != 0) {
            watchdog_checkin(WDT_TX);
            continue;
        }
        if (ble_audio_send(tf.data, tf.len) != 0) {
            notify_drops++;
            if (g_state.backlog_mode && qspi_store_ready()) {
                qspi_store_push(tf.data, tf.len);
            }
        }
        watchdog_checkin(WDT_TX);
    }
}

K_THREAD_DEFINE(tx_tid, 2048, tx_thread, NULL, NULL, NULL, 6, 0, 0);

/* Frames held back from just before the voice gate opened.
 *
 * The gate opens on the first frame whose level crosses the threshold, and
 * everything before it was dropped -- so every utterance lost its onset. The
 * quiet part of a word carries a lot of what makes it recognisable, and the
 * transcriber was being handed speech with the beginnings shaved off. Arduino
 * already kept a pre-roll; this side did not.
 *
 * Built frames rather than PCM, so the sequence number and the capture
 * timestamp are the ones the frame was made with. Re-encoding at flush time
 * would date the audio to the moment the gate opened, which is the bug this
 * exists to avoid. Four frames is 80 ms.
 */
#define PREROLL_FRAMES 4
static uint8_t  preroll[PREROLL_FRAMES][MAX_FRAME_LEN];
static uint16_t preroll_len[PREROLL_FRAMES];
static uint8_t  preroll_head, preroll_count;

static void preroll_stash(const uint8_t *wire, uint16_t len)
{
    memcpy(preroll[preroll_head], wire, len);
    preroll_len[preroll_head] = len;
    preroll_head = (preroll_head + 1) % PREROLL_FRAMES;
    if (preroll_count < PREROLL_FRAMES) {
        preroll_count++;
    }
}

static void route_frame(const uint8_t *wire, uint16_t len);

static void preroll_flush(void)
{
    uint8_t start = (preroll_head - preroll_count + PREROLL_FRAMES) % PREROLL_FRAMES;

    for (uint8_t i = 0; i < preroll_count; i++) {
        uint8_t idx = (start + i) % PREROLL_FRAMES;
        route_frame(preroll[idx], preroll_len[idx]);
    }
    preroll_count = 0;
}

/* Send one built frame, or store it, whichever the link allows.
 *
 * Lifted out of the capture loop so that pre-roll frames take exactly the
 * same path as live ones -- a second copy of this routing would drift from
 * the first, and the flash/queue/drop decisions here are the ones that were
 * hardest to get right.
 */
static void route_frame(const uint8_t *wire, uint16_t len)
{
    if (!ble_audio_ready()) {
        /* Nobody to send to. Buffer rather than discard: the point of the
         * flash is that walking out of range costs latency, not audio. */
        if (g_state.backlog_mode && qspi_store_ready()) {
            qspi_store_push(wire, (uint8_t)len);
        }
        return;
    }

    /* With a backlog outstanding, the frame just captured joins the back of
     * the queue rather than going straight out: interleaving live audio with
     * replayed audio splices two different moments of the conversation
     * together. The writer thread replays the queue in order. Strict ordering
     * costs latency, which is the trade the flash buffer exists to make. */
    if (qspi_store_pending() > 0 && g_state.backlog_mode &&
        qspi_store_ready()) {
        qspi_store_push(wire, (uint8_t)len);
        /* This branch never touches the radio, so a counter of failed sends
         * cannot see a link that has died underneath it. Say that we tried;
         * the guard measures how long since anything actually left. */
        ble_audio_note_delivery_attempt();
        return;
    }

    /* A frame the radio would not take goes to the flash instead, if the
     * backlog is enabled; otherwise it is counted as dropped and the gap is
     * visible to the host. */
    struct txframe tf;

    tf.len = (uint8_t)len;
    memcpy(tf.data, wire, len);
    if (k_msgq_put(&tx_q, &tf, K_NO_WAIT) != 0) {
        /* The radio is behind. Spill rather than wait: blocking here is what
         * starved the microphone. */
        notify_drops++;
        if (g_state.backlog_mode && qspi_store_ready()) {
            qspi_store_push(wire, (uint8_t)len);
        }
    }
}

static int16_t raw[MAX_SAMPLES];
static int16_t frame[MAX_SAMPLES];
static uint8_t wire[MAX_FRAME_LEN];

static void capture_fn(void *a, void *b, void *c)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(c);
    uint16_t seq = 0;
    int hangover = 0;

    while (1) {
        watchdog_checkin(WDT_CAPTURE);

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
            count = decimate_2to1(raw, got, frame);
        } else {
            memcpy(frame, raw, got * sizeof(int16_t));
        }

        /* Gate on voice while buffering, whatever the live setting says.
         * With nobody listening, silence still costs a flash write, wear, and
         * the time to replay it later -- 19 of 40 recovered clips in one
         * session were silence. When a host is attached the user's setting
         * stands, because then silence costs only radio. */
        bool gate = g_state.vad_enabled ||
                    (g_state.backlog_mode && !ble_audio_ready());
        bool voiced = true;
        if (!gate) {
            /* Nothing held back is still current once the gate is off; a
             * frame kept from an earlier gated stretch would be flushed into
             * the middle of unrelated audio if the gate came back on. */
            preroll_count = 0;
        }
        if (gate) {
            uint32_t rms = codec_rms(frame, count);
            if (rms >= g_state.vad_thresh) {
                hangover = 15;              /* 300 ms release */
            } else if (hangover > 0) {
                hangover--;
            }
            voiced = hangover > 0;
            if (!voiced) {
                /* Keep it in case the gate opens on the next frame. Built
                 * here so it carries this frame's own sequence number and
                 * timestamp; encoding it later would date it wrongly. */
                uint8_t qflags = FLAG_VAD_ON;
                if (g_state.use16k) {
                    qflags |= FLAG_16K;
                }
                uint16_t qlen = codec_build_frame(frame, count, seq,
                                                  k_uptime_get_32(), qflags,
                                                  wire);
                preroll_stash(wire, qlen);
                seq++;                      /* the gap is intentional, not loss */
                continue;
            }
        }

        uint8_t flags = 0;
        if (g_state.use16k)      flags |= FLAG_16K;
        if (voiced)              flags |= FLAG_VOICED;
        if (gate)                flags |= FLAG_VAD_ON;

        uint16_t len = codec_build_frame(frame, count, seq,
                                         k_uptime_get_32(), flags, wire);

        /* Anything held back from just before the gate opened goes first, so
         * the onset of the word arrives ahead of the rest of it. */
        if (preroll_count) {
            preroll_flush();
        }
        route_frame(wire, len);
        seq++;
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

    /* Print why the last boot ended before anything else can overwrite it.
     * Several evenings of this project were spent unable to tell a reset
     * from a hang, which the reset register answers immediately. */
    report_reset_reason();

    int led_err = led_init();
    led_set_level(g_state.led_level);
    led_set_mode(g_state.led_mode);
    led_set_colour(false, false, true);
    LOG_INF("Boswell starting");
    LOG_INF("led_init -> %d (ready=%d)", led_err, led_ready());
    LOG_INF("shell ready: 'boswell dfu' reboots for flashing");

    int err = mic_init();
    LOG_INF("mic_init -> %d", err);

    err = ble_audio_init(on_ctrl);
    LOG_INF("ble_audio_init -> %d", err);

    qspi_store_set_drain(drain_to_host, ble_audio_ready);
    qspi_store_set_alive_cb(qspi_alive);
    err = cfg_store_init();
    LOG_INF("cfg_store_init -> %d", err);

    err = qspi_store_init();
    LOG_INF("qspi_store_init -> %d", err);
    if (err == 0) {
        watchdog_expect(WDT_QSPI);
    } else {
        LOG_WRN("no flash backlog; watchdog will not wait for it");
    }

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

    k_thread_create(&imu_thread, imu_stack, IMU_STACK,
                    imu_stream_fn, NULL, NULL, NULL,
                    K_PRIO_PREEMPT(11), 0, K_NO_WAIT);
    k_thread_name_set(&imu_thread, "imu");

    k_thread_create(&capture_thread, capture_stack, CAPTURE_STACK,
                    capture_fn, NULL, NULL, NULL,
                    /* Preemptible, and deliberately below the Bluetooth RX
                     * thread. As a cooperative thread it outranked the host
                     * stack and could only be descheduled by blocking. */
                    K_PRIO_PREEMPT(10), 0, K_NO_WAIT);
    k_thread_name_set(&capture_thread, "capture");

    /* Ticks at the pulse resolution rather than the housekeeping interval: a
     * 25 ms flash cannot be driven by a loop that wakes twice a second. */
    int64_t next_house = 0, next_batt = 0, next_motion = 0;
    while (1) {
        int64_t now = k_uptime_get();

        led_service();
        usb_service();

        if (now >= next_house) {
            next_house = now + 500;
            watchdog_checkin(WDT_MAIN);
            watchdog_service();
            led_state();
        }
        if (now >= next_batt) {
            next_batt = now + 30000;
            battery_sample();
        }
        if (now >= next_motion) {
            /* Every couple of seconds: the step counter runs in the part and
             * only needs collecting, and tilt and significant motion latch
             * until read so nothing is missed between polls. */
            next_motion = now + 2000;
            imu_motion_poll();
        }
        struct boswell_settings cur;
        settings_snapshot(&cur);
        cfg_store_service(&cur);
        k_sleep(K_MSEC(10));
    }
    return 0;
}
