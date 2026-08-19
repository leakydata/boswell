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
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(ble_audio, LOG_LEVEL_INF);

static struct bt_uuid_128 svc_uuid   = BT_UUID_INIT_128(BOSWELL_UUID_SERVICE);
static struct bt_uuid_128 audio_uuid = BT_UUID_INIT_128(BOSWELL_UUID_AUDIO);
static struct bt_uuid_128 ctrl_uuid  = BT_UUID_INIT_128(BOSWELL_UUID_CTRL);
static struct bt_uuid_128 info_uuid  = BT_UUID_INIT_128(BOSWELL_UUID_INFO);

static struct bt_conn *current_conn;
static bool notify_enabled;
static ctrl_handler_t ctrl_cb;
static uint8_t info_buf[40];

static void apply_conn_params(bool streaming);

static void ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
    ARG_UNUSED(attr);
    notify_enabled = (value == BT_GATT_CCC_NOTIFY);
    LOG_INF("notifications %s", notify_enabled ? "on" : "off");
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
                           BT_GATT_PERM_WRITE, NULL, ctrl_write, NULL),
    BT_GATT_CHARACTERISTIC(&info_uuid.uuid, BT_GATT_CHRC_READ,
                           BT_GATT_PERM_READ, info_read, NULL, NULL),
);

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

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(conn);
    LOG_INF("disconnected (0x%02x)", reason);
    if (current_conn) {
        bt_conn_unref(current_conn);
        current_conn = NULL;
    }
    notify_enabled = false;
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

int ble_audio_send(const uint8_t *frame, uint16_t len)
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
        int err = bt_gatt_notify(current_conn, &boswell_svc.attrs[1], frame, len);
        if (err != -ENOMEM && err != -EAGAIN) {
            return err;
        }
        k_sleep(K_MSEC(2));
    }
    LOG_WRN("notify queue stayed full; dropping a frame");
    return -ENOMEM;
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
    info_buf[5] = (uint8_t)(g_state.vad_enabled | (g_state.backlog_mode << 1));
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
    info_buf[32] = g_state.led_level;
    info_buf[33] = g_state.led_mode;
    uint16_t mv = battery_mv();
    info_buf[34] = (uint8_t)(mv & 0xFF);
    info_buf[35] = (uint8_t)(mv >> 8);
    info_buf[36] = battery_percent();
    info_buf[37] = (uint8_t)((battery_charging() ? 1 : 0) | (mic_running() ? 4 : 0));
    info_buf[39] = (uint8_t)g_state.tx_power;
}

int ble_audio_init(ctrl_handler_t on_ctrl)
{
    ctrl_cb = on_ctrl;

    int err = bt_enable(NULL);
    if (err) {
        LOG_ERR("bt_enable failed (%d)", err);
        return err;
    }
    ble_audio_publish_info();

    err = bt_le_adv_start(&adv_param, adv, ARRAY_SIZE(adv),
                          scan_rsp, ARRAY_SIZE(scan_rsp));
    if (err) {
        LOG_ERR("advertising failed (%d)", err);
        return err;
    }
    LOG_INF("advertising as %s", CONFIG_BT_DEVICE_NAME);
    return 0;
}
