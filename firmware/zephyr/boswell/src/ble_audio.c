/*
 * GATT service carrying the audio stream.
 *
 * UUIDs, characteristic layout and control opcodes are identical to the
 * Arduino build, so the host cannot tell which firmware is running. That is
 * the contract that lets both live in this repository without becoming two
 * separate projects.
 */

#include "ble_audio.h"
#include "imu_tap.h"
#include "battery.h"
#include "mic.h"
#include "qspi_store.h"

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/random/random.h>
#include <zephyr/settings/settings.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(ble_audio, LOG_LEVEL_INF);

static struct bt_uuid_128 svc_uuid   = BT_UUID_INIT_128(BOSWELL_UUID_SERVICE);
static struct bt_uuid_128 audio_uuid = BT_UUID_INIT_128(BOSWELL_UUID_AUDIO);
/* Whether a control write needs an encrypted link.
 *
 * The control characteristic arms the microphone, erases the backlog and can
 * reboot into the bootloader, so leaving it open means anyone in radio range
 * can do those things to a microphone somebody is wearing. That is the threat
 * model, stated rather than implied.
 *
 * It ships open, and that is a decision rather than an oversight. Requiring
 * encryption was built and tried on real hardware: the board enforced it
 * correctly, and the host could not get past it. This board has no display or
 * keypad, so pairing is Just Works, and Just Works pairing on a headless
 * Linux host needs a BlueZ agent that is not there -- bleak's pair() hangs
 * until it is killed. An always-on recorder that stops recording is a worse
 * outcome than one a neighbour could theoretically disarm.
 *
 * Set this to 1 to require encryption. Everything it needs is already
 * configured: CONFIG_BT_SMP, bonding, and persisted keys. Pair once from a
 * host with a working agent -- bluetoothctl in an interactive session does
 * have one -- and "boswell unpair" is the way back if it goes wrong. The USB
 * shell can arm and disarm regardless, which is the real safety net.
 *
 * Just Works also means the encryption is unauthenticated: it stops a
 * passer-by issuing commands, and does not stop someone present at the moment
 * of pairing. Worth knowing before relying on it.
 */
#define BOSWELL_SECURE_CTRL 0

#if BOSWELL_SECURE_CTRL
#define BOSWELL_CTRL_PERM BT_GATT_PERM_WRITE_ENCRYPT
#else
#define BOSWELL_CTRL_PERM BT_GATT_PERM_WRITE
#endif

static struct bt_uuid_128 ctrl_uuid  = BT_UUID_INIT_128(BOSWELL_UUID_CTRL);
static struct bt_uuid_128 info_uuid  = BT_UUID_INIT_128(BOSWELL_UUID_INFO);
static struct bt_uuid_128 imu_uuid   = BT_UUID_INIT_128(BOSWELL_UUID_IMU);

static struct bt_conn *current_conn;
static bool advertising;

/* Drops a central that connects and never subscribes.
 *
 * With one connection slot, a link that is not carrying audio is worse than
 * no link: it blocks the real host from connecting AND stops advertising, so
 * the device is unreachable while still believing it is connected. A stale
 * BlueZ connection left by a test client did exactly that -- the board sat
 * linked to nothing for three hours, buffering, with no way in.
 *
 * A host that means to listen subscribes within a second or two, so half a
 * minute is generous.
 */
#define SUBSCRIBE_GRACE_MS 30000
static uint32_t idle_arms, idle_fires, idle_drops;
/* Identifies this boot, so the host can tell the device clock restarted. */
static uint16_t boot_id;
static void idle_link_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(idle_link, idle_link_fn);
static bool notify_enabled;
static bool imu_notify_enabled;
static ctrl_handler_t ctrl_cb;
static uint8_t info_buf[40];

static void apply_conn_params(bool streaming);

static void imu_ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    imu_notify_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("imu notifications %s", imu_notify_enabled ? "on" : "off");
}

static void ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("notifications %s", notify_enabled ? "on" : "off");
    if (notify_enabled) {
        k_work_cancel_delayable(&idle_link);
    } else if (current_conn != NULL) {
        /* Subscribed and then stopped, without disconnecting. The guard was
         * cancelled when the subscription arrived and nothing re-armed it, so
         * a link that went quiet after subscribing was never reclaimed --
         * which is how the board sat linked-but-silent for a hundred seconds
         * with the guard armed and idle. Arming on connect alone is not
         * enough; the condition to watch for is "linked and not carrying
         * audio", however it got there. */
        idle_arms++;
        k_work_reschedule(&idle_link, K_MSEC(SUBSCRIBE_GRACE_MS));
    }
    /* Deliberately does not renegotiate here. The host subscribes and then
     * immediately sends CTRL_STREAM, and Zephyr rejects a second parameter
     * update while the first is still in flight (-EBUSY). Firing one here
     * meant the streaming request was silently dropped and the link ran the
     * whole capture at the 200 ms idle interval. */
}

static ssize_t ctrl_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                          const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
    ARG_UNUSED(conn); ARG_UNUSED(attr); ARG_UNUSED(offset); ARG_UNUSED(flags);
    const uint8_t *p = buf;
    if (len < 2) {
        return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
    }
    if (ctrl_cb) {
        ctrl_cb(p[0], p[1]);
    }
    return len;
}

static ssize_t info_read(struct bt_conn *conn, const struct bt_gatt_attr *attr,
                         void *buf, uint16_t len, uint16_t offset)
{
    /* Refresh on read rather than serving whatever was last published. The
     * backlog size and battery move continuously, and a host that connected
     * to a device with 93 KB queued was being told the backlog was empty. */
    if (offset == 0) {
        ble_audio_publish_info();
    }
    return bt_gatt_attr_read(conn, attr, buf, len, offset,
                             info_buf, sizeof(info_buf));
}

BT_GATT_SERVICE_DEFINE(boswell_svc,
    BT_GATT_PRIMARY_SERVICE(&svc_uuid),
    BT_GATT_CHARACTERISTIC(&audio_uuid.uuid, BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_NONE, NULL, NULL, NULL),
    BT_GATT_CCC(ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
    BT_GATT_CHARACTERISTIC(&ctrl_uuid.uuid,
                           BT_GATT_CHRC_WRITE | BT_GATT_CHRC_WRITE_WITHOUT_RESP,
                           BOSWELL_CTRL_PERM, NULL, ctrl_write, NULL),
    BT_GATT_CHARACTERISTIC(&info_uuid.uuid, BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ, info_read, NULL, NULL),
    BT_GATT_CHARACTERISTIC(&imu_uuid.uuid, BT_GATT_CHRC_NOTIFY,
                           BT_GATT_PERM_NONE, NULL, NULL, NULL),
    BT_GATT_CCC(imu_ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* The value attributes to notify on, found by UUID rather than counted.
 *
 * These were boswell_svc.attrs[1] for audio and attrs[8] for motion. The
 * indices are correct only for the exact declaration above, and adding a
 * characteristic or a descriptor anywhere before them shifts every later one
 * -- silently, because bt_gatt_notify on the wrong attribute does not fail,
 * it just sends audio to whoever subscribed to something else. Nothing about
 * the resulting bug would point at this line.
 *
 * Resolved once at init and checked, so a mistake is a refusal to start
 * rather than a stream that goes somewhere unexpected.
 */
static const struct bt_gatt_attr *audio_attr;
static const struct bt_gatt_attr *imu_attr;

static const struct bt_gatt_attr *find_value_attr(const struct bt_uuid *uuid)
{
    /* The value attribute is the one after the characteristic declaration. */
    for (size_t i = 0; i + 1 < boswell_svc.attr_count; i++) {
        const struct bt_gatt_attr *a = &boswell_svc.attrs[i];

        if (bt_uuid_cmp(a->uuid, BT_UUID_GATT_CHRC) == 0) {
            const struct bt_gatt_chrc *chrc = a->user_data;

            if (chrc && bt_uuid_cmp(chrc->uuid, uuid) == 0) {
                return &boswell_svc.attrs[i + 1];
            }
        }
    }
    return NULL;
}

static const struct bt_data adv[] = {
    BT_DATA_BYTES(BT_DATA_FLAGS, (BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR)),
    BT_DATA(BT_DATA_NAME_COMPLETE, CONFIG_BT_DEVICE_NAME,
            sizeof(CONFIG_BT_DEVICE_NAME) - 1),
};
/* Spelled out rather than using a convenience macro: the shorthand names for
 * connectable advertising have changed across Zephyr releases. */
static const struct bt_le_adv_param adv_param = BT_LE_ADV_PARAM_INIT(
    BT_LE_ADV_OPT_CONNECTABLE,
    BT_GAP_ADV_FAST_INT_MIN_2, BT_GAP_ADV_FAST_INT_MAX_2, NULL);

static const struct bt_data scan_rsp[] = {
    BT_DATA_BYTES(BT_DATA_UUID128_ALL, BOSWELL_UUID_SERVICE),
};

/* Audio wants a tight connection interval; an idle link does not, and holding
 * one spends radio power on nothing. */
void ble_audio_apply_conn_params(bool streaming)
{
    apply_conn_params(streaming);
}

static void apply_conn_params(bool streaming)
{
    if (!current_conn) {
        return;
    }
    struct bt_le_conn_param p = streaming
        ? *BT_LE_CONN_PARAM(6, 12, 0, 400)      /* 7.5-15 ms */
        : *BT_LE_CONN_PARAM(80, 160, 0, 400);   /* 100-200 ms */
    int err = bt_conn_le_param_update(current_conn, &p);
    if (err) {
        LOG_WRN("conn param update (%u-%u) rejected: %d",
                p.interval_min, p.interval_max, err);
    } else {
        LOG_INF("requested interval %u-%u", p.interval_min, p.interval_max);
    }
}

static void connected(struct bt_conn *conn, uint8_t err)
{
    if (err) {
        LOG_ERR("connect failed (0x%02x)", err);
        return;
    }
    current_conn = bt_conn_ref(conn);
    advertising = false;
    idle_arms++;
    k_work_reschedule(&idle_link, K_MSEC(SUBSCRIBE_GRACE_MS));
    struct bt_conn_info info;
    if (bt_conn_get_info(conn, &info) == 0) {
        LOG_INF("connected: interval %u, latency %u, timeout %u",
                info.le.interval, info.le.latency, info.le.timeout);
    } else {
        LOG_INF("connected");
    }
    /* Deliberately does NOT touch connection parameters here. The central is
     * still doing ATT service discovery, and dropping to a 200 ms interval
     * mid-discovery stretches it past the host's connect timeout, so the link
     * comes up and then appears to hang. Renegotiate once the client
     * subscribes, which means discovery has finished. */
}

/* Advertising has to be restarted by hand after a disconnect.
 *
 * Without this the board went quiet the moment a host dropped: not connected
 * and not advertising, so nothing could ever reach it again until it was
 * power-cycled. Everything captured from then on went to flash, which is why
 * almost every clip arrived as recovered audio rather than live.
 *
 * Done from a work item rather than inside the callback, because the
 * connection is not fully torn down at that point and starting an advertiser
 * there can be rejected. */
int ble_audio_advertise_now(void)
{
    /* Stop first. Zephyr answers -EALREADY when it still holds an advertiser
     * set from before a connection, and treating that as success left the
     * board neither connected nor advertising -- unreachable until it was
     * power-cycled. Stopping is harmless when nothing is running. */
    (void)bt_le_adv_stop();
    advertising = false;

    int err = bt_le_adv_start(&adv_param, adv, ARRAY_SIZE(adv),
                              scan_rsp, ARRAY_SIZE(scan_rsp));
    if (err) {
        LOG_ERR("advertising failed (%d)", err);
        return err;
    }
    advertising = true;
    LOG_INF("advertising");
    return 0;
}

bool ble_audio_advertising(void)
{
    return advertising && current_conn == NULL;
}

static void idle_link_fn(struct k_work *work)
{
    ARG_UNUSED(work);
    idle_fires++;
    if (current_conn == NULL || notify_enabled) {
        return;
    }
    LOG_WRN("central never subscribed; dropping the link");
    int err = bt_conn_disconnect(current_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    if (err) {
        LOG_ERR("disconnect failed (%d); retrying", err);
        k_work_reschedule(k_work_delayable_from_work(work), K_MSEC(2000));
        return;
    }
    idle_drops++;
}

void ble_audio_idle_stats(uint32_t out[3])
{
    out[0] = idle_arms; out[1] = idle_fires; out[2] = idle_drops;
}

static void adv_restart_fn(struct k_work *work)
{
    if (current_conn != NULL) {
        return;                      /* somebody got in first */
    }
    if (ble_audio_advertise_now() != 0) {
        k_work_reschedule(k_work_delayable_from_work(work), K_MSEC(1000));
    }
}
static K_WORK_DELAYABLE_DEFINE(adv_restart, adv_restart_fn);

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(conn);
    LOG_INF("disconnected (0x%02x)", reason);
    if (current_conn) {
        bt_conn_unref(current_conn);
        current_conn = NULL;
    }
    notify_enabled = false;
    imu_notify_enabled = false;
    k_work_cancel_delayable(&idle_link);
    k_work_reschedule(&adv_restart, K_MSEC(100));
    /* Deliberately stays armed: capture continuing across a dropped link is
     * the whole point of the flash buffer. */
}

static bool le_param_req(struct bt_conn *conn, struct bt_le_conn_param *param)
{
    ARG_UNUSED(conn);
    LOG_INF("param request: interval %u-%u latency %u timeout %u",
            param->interval_min, param->interval_max,
            param->latency, param->timeout);
    return true;
}

static void le_param_updated(struct bt_conn *conn, uint16_t interval,
                             uint16_t latency, uint16_t timeout)
{
    ARG_UNUSED(conn);
    LOG_INF("params now: interval %u latency %u timeout %u",
            interval, latency, timeout);
}

/* Logged so the negotiated link is a measurement rather than an assumption:
 * a 4.0 dongle, an AX210 and a phone all end up in different places here. */
static void le_phy_updated(struct bt_conn *conn, struct bt_conn_le_phy_info *info)
{
    ARG_UNUSED(conn);
    LOG_INF("phy now: tx %u rx %u", info->tx_phy, info->rx_phy);
}

static void le_data_len_updated(struct bt_conn *conn,
                                struct bt_conn_le_data_len_info *info)
{
    ARG_UNUSED(conn);
    LOG_INF("data len now: tx %u rx %u", info->tx_max_len, info->rx_max_len);
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected = connected,
    .disconnected = disconnected,
    .le_param_req = le_param_req,
    .le_param_updated = le_param_updated,
    .le_phy_updated = le_phy_updated,
    .le_data_len_updated = le_data_len_updated,
};

bool ble_audio_connected(void)
{
    return current_conn != NULL && notify_enabled;
}

bool ble_imu_ready(void)
{
    return current_conn != NULL && imu_notify_enabled;
}

int ble_imu_send(const uint8_t *frame, uint16_t len)
{
    if (!ble_imu_ready()) {
        return -ENOTCONN;
    }
    /* Index 8, counting the table above: primary service, then two
     * attributes for each characteristic and one for each CCC. Attribute 6
     * is the info declaration, which is where a first attempt at this sent
     * motion frames.
     *
     * One attempt only. Audio retries because a gap in speech matters; a
     * missing twentieth of a second of accelerometer does not, and blocking
     * the sampler to retry would skew the timing of everything after it. */
    return bt_gatt_notify(current_conn, imu_attr, frame, len);
}

bool ble_audio_linked(void)
{
    return current_conn != NULL;
}

/* How long notifications actually take, and how often they have to wait.
 * Capture ran at 58 frames a second into flash while only about one a second
 * reached the host, which puts the cost here and nowhere else. */
static uint32_t send_calls, send_retries, send_us_max, send_us_total;

void ble_audio_send_stats(uint32_t out[4])
{
    out[0] = send_calls; out[1] = send_retries;
    out[2] = send_us_max; out[3] = send_calls ? send_us_total / send_calls : 0;
}

static int ble_audio_send_inner(const uint8_t *frame, uint16_t len)
{
    if (!ble_audio_connected()) {
        return -ENOTCONN;
    }
    /* Attribute 1 is the audio value.
     *
     * A full transmit queue returns -ENOMEM. Returning that to the caller and
     * moving on drops the frame, and because the sequence number does not
     * advance the host sees no gap -- it reported zero loss while half the
     * audio never left the device. Wait for a slot instead: the microphone
     * slab holds about a second, so brief backpressure costs latency rather
     * than audio. */
    for (int attempt = 0; attempt < 20; attempt++) {
        if (attempt) {
            send_retries++;
        }
        int err = bt_gatt_notify(current_conn, audio_attr, frame, len);
        if (err != -ENOMEM && err != -EAGAIN) {
            return err;
        }
        k_sleep(K_MSEC(2));
    }
    LOG_WRN("notify queue stayed full; dropping a frame");
    return -ENOMEM;
}

/* A link that stops accepting audio is a link that is gone.
 *
 * Observed: the host process was killed, the controller showed no connection
 * at all, and the firmware went on believing it had a subscribed central for
 * forty minutes. It never re-advertised, so nothing could reach it, and the
 * flash filled and then dropped 8011 frames. The subscribe guard did not help
 * -- that one only watches for a central that never subscribes, and this one
 * had.
 *
 * Every notification during those forty minutes was failing, which is why the
 * backlog grew, so the evidence was already here and nothing acted on it.
 * Five seconds of audio in which not one frame reaches the radio is a dead
 * link by any useful definition; drop it and advertise, so the host can find
 * the device again and drain what was buffered. */
#define DEAD_LINK_FRAMES 250       /* at 20 ms a frame, about five seconds */
/* Nothing delivered at all for this long, while the firmware believes it has
 * a link, means the link is not there. Sixty seconds is far longer than any
 * backlog burst and far shorter than the forty minutes this went unnoticed. */
#define DEAD_LINK_SILENT_MS 60000
static uint32_t consecutive_fails;
static uint32_t dead_link_drops;
static int64_t  last_delivery_ms;

int ble_audio_conn_handle(void)
{
    uint16_t h;

    if (current_conn == NULL || bt_hci_get_conn_handle(current_conn, &h) != 0) {
        return -1;
    }
    return (int)h;
}

void ble_audio_dead_link_stats(uint32_t out[3])
{
    out[0] = consecutive_fails;
    out[1] = dead_link_drops;
    out[2] = (current_conn && last_delivery_ms)
             ? (uint32_t)(k_uptime_get() - last_delivery_ms) / 1000 : 0;
}

/* Called from the writer thread as it tries to drain.
 *
 * The send-failure counter above only sees frames that were offered to the
 * radio, and in backlog mode the capture thread does not offer them -- it
 * pushes straight to flash for ordering and lets the writer drain. So during
 * the outage no send ever failed, because no send was ever attempted, and a
 * counter of failures could not have noticed. What was observable is that
 * nothing left the device at all while the firmware insisted it had a
 * subscribed central. That is what this checks. */
void ble_audio_note_delivery_attempt(void)
{
    if (current_conn == NULL) {
        last_delivery_ms = 0;
        return;
    }
    if (last_delivery_ms == 0) {
        last_delivery_ms = k_uptime_get();
        return;
    }
    if (k_uptime_get() - last_delivery_ms < DEAD_LINK_SILENT_MS) {
        return;
    }
    LOG_WRN("nothing delivered for %d s on a link we believe is up; dropping it",
            DEAD_LINK_SILENT_MS / 1000);
    if (bt_conn_disconnect(current_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN) == 0) {
        dead_link_drops++;
    }
    last_delivery_ms = 0;
}

int ble_audio_send(const uint8_t *frame, uint16_t len)
{
    uint32_t t0 = k_cycle_get_32();
    int rc = ble_audio_send_inner(frame, len);

    if (rc == 0) {
        consecutive_fails = 0;
        last_delivery_ms = k_uptime_get();
    } else if (current_conn && ++consecutive_fails == DEAD_LINK_FRAMES) {
        LOG_WRN("no frame has reached the radio in %u tries; dropping the link",
                consecutive_fails);
        if (bt_conn_disconnect(current_conn,
                               BT_HCI_ERR_REMOTE_USER_TERM_CONN) == 0) {
            dead_link_drops++;
        }
        consecutive_fails = 0;
    }
    uint32_t us = k_cyc_to_us_floor32(k_cycle_get_32() - t0);

    send_calls++;
    send_us_total += us;
    if (us > send_us_max) {
        send_us_max = us;
    }
    return rc;
}

bool ble_audio_ready(void)
{
    return current_conn != NULL && notify_enabled;
}

void ble_audio_publish_info(void)
{
    uint16_t ns = g_state.use16k ? MAX_SAMPLES : MAX_SAMPLES / 2;
    memset(info_buf, 0, sizeof(info_buf));
    info_buf[0] = 1;                       /* codec: IMA ADPCM */
    info_buf[1] = g_state.use16k;
    info_buf[2] = PROTO_FRAME_MS;
    info_buf[3] = ns & 0xFF;
    info_buf[4] = (ns >> 8) & 0xFF;
    /* Bit 2 is whether capture is actually running. The host kept its own
     * idea of "armed" and had no way to check it: after a reconnect it
     * believed capture was on while the device had booted with it off, and
     * nothing was being recorded at all. */
    /* One coherent look at the group, not three separate ones. */
    k_mutex_lock(&g_state_lock, K_FOREVER);
    info_buf[5] = (uint8_t)(g_state.vad_enabled
                            | (g_state.backlog_mode << 1)
                            | (g_state.streaming ? 4 : 0));
    k_mutex_unlock(&g_state_lock);
    /* 1 = the internal sensor bus. The host prints four WHO_AM_I probe slots
     * because the Arduino build has two candidate buses to search; Zephyr
     * routes the sensors to i2c0 only, so the last two stay unprobed. */
    info_buf[6] = imu_tap_present() ? 1 : 0;
    info_buf[7] = imu_tap_addr();
    imu_tap_probe(&info_buf[8]);
    uint32_t pend = qspi_store_pending();
    info_buf[27] = qspi_store_ready() ? 1 : 0;
    info_buf[28] = (uint8_t)(pend & 0xFF);
    info_buf[29] = (uint8_t)((pend >> 8) & 0xFF);
    info_buf[30] = (uint8_t)((pend >> 16) & 0xFF);
    info_buf[31] = (uint8_t)(qspi_store_capacity() / 65536);
    /* Steps and motion. Bytes 13-17 were unused; the host reads them as a
     * little-endian count and a flags byte. */
    uint32_t steps = imu_steps();
    info_buf[13] = (uint8_t)(steps & 0xFF);
    info_buf[14] = (uint8_t)((steps >> 8) & 0xFF);
    info_buf[15] = (uint8_t)((steps >> 16) & 0xFF);
    info_buf[16] = (uint8_t)((steps >> 24) & 0xFF);
    info_buf[17] = (uint8_t)((imu_tilt() ? 1 : 0) |
                             (imu_significant_motion() ? 2 : 0) |
                             (imu_tap_enabled() ? 4 : 0));
    /* Tell the host what it is talking to, so it does not have to guess
     * which firmware wrote bytes 13-26 or whether byte 38 means anything. */
    info_buf[18] = INFO_VERSION;
    info_buf[19] = INFO_FW_ZEPHYR;
    uint16_t caps = INFO_CAP_STEPS | INFO_CAP_IMU_RAW | INFO_CAP_FLASH |
                    INFO_CAP_OTA | INFO_CAP_STATE | INFO_CAP_BOOTID |
                    INFO_CAP_SPLITBUF;
    info_buf[20] = (uint8_t)(caps & 0xFF);
    info_buf[21] = (uint8_t)(caps >> 8);
    /* Which boot this is.
     *
     * Frame timestamps come from k_uptime_get_32(), which restarts at zero on
     * every reboot. The host maps those to wall-clock time using an anchor it
     * captured earlier, so after a reboot it maps fresh audio to a moment in
     * the past -- and clip ordering, which is the whole reason the device
     * publishes timestamps at all, silently goes wrong. There is nothing in
     * the frame that says "this is a different clock now". This is that.
     *
     * Random rather than a counter: a counter needs somewhere durable to live
     * and would still repeat after a factory erase, and the host only needs
     * to know the value changed, not what it counts. */
    info_buf[22] = (uint8_t)(boot_id & 0xFF);
    info_buf[23] = (uint8_t)(boot_id >> 8);
    info_buf[32] = g_state.led_level;
    info_buf[33] = g_state.led_mode;
    uint16_t mv = battery_mv();
    info_buf[34] = (uint8_t)(mv & 0xFF);
    info_buf[35] = (uint8_t)(mv >> 8);
    info_buf[36] = battery_percent();
    info_buf[37] = (uint8_t)((battery_charging() ? 1 : 0)
                             | (battery_fast_charge() ? 2 : 0)
                             | (mic_running() ? 4 : 0));
    info_buf[39] = (uint8_t)g_state.tx_power;
}

int ble_audio_init(ctrl_handler_t on_ctrl)
{
    audio_attr = find_value_attr(&audio_uuid.uuid);
    imu_attr   = find_value_attr(&imu_uuid.uuid);
    if (audio_attr == NULL || imu_attr == NULL) {
        LOG_ERR("audio or motion characteristic missing from the service");
        return -ENOENT;
    }

    /* A value that will not repeat across a reboot. sys_rand32_get() is
     * seeded by the SoC's entropy source; zero is excluded so the host can
     * treat it as "not published". */
    do {
        boot_id = (uint16_t)sys_rand32_get();
    } while (boot_id == 0);

    ctrl_cb = on_ctrl;

    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (%d)", err);
        return err;
    }

    /* Required once bonds are stored.
     *
     * With CONFIG_BT_SETTINGS the host will not start advertising until the
     * settings subsystem has handed it whatever keys it has -- so leaving
     * this out does not fail loudly, it just means the device never appears.
     * Which is precisely what it did: the application ran, the shell
     * answered, and nothing could find it over the radio.
     */
    err = settings_load();
    if (err) {
        LOG_ERR("settings_load failed (%d); bonds will not persist", err);
    }

    ble_audio_publish_info();

    err = ble_audio_advertise_now();
    if (err) {
        return err;
    }
    LOG_INF("advertising as %s", CONFIG_BT_DEVICE_NAME);
    return 0;
}
