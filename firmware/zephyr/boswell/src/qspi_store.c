#include "qspi_store.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/ring_buffer.h>
#include <zephyr/pm/device.h>

LOG_MODULE_REGISTER(qspi, LOG_LEVEL_INF);

#define PAGE   256
#define SECTOR 4096

static const struct device *flash_dev;
static bool     ready;
static uint32_t capacity;

/* Monotonic byte positions, wrapped only when addressing flash. Tracking raw
 * offsets instead made "has the writer lapped the reader" ambiguous at the
 * wrap, and the first version dropped the whole buffer on its first page:
 * with w_off and r_off both 0 the erase-ahead looked like a lap. Positions
 * that only ever increase remove the ambiguity rather than special-casing it. */
static int64_t  w_pos;          /* total bytes written  */
static int64_t  r_pos;          /* total bytes read     */
static int64_t  erased_pos;     /* erased up to here    */
static uint32_t dropped;

/* Word-aligned: the QSPI DMA cannot read from anywhere else, and the driver's
 * fallback for that is one transfer per four bytes. */
static uint8_t  page_buf[PAGE] __aligned(4);
static uint32_t page_fill;      /* bytes staged for the page at w_off */

static struct k_mutex lock;
/* Length of the record last handed out by peek and not yet committed. */
static uint8_t peeked_len;

/* Staging ring between the capture thread and flash.
 *
 * Flash writes cannot happen on the capture thread. A sector erase on this
 * part blocks for tens of milliseconds, which is long enough for the DMIC
 * slab to overrun, and Zephyr's PDM driver does not recover from that: it
 * reports "No audio data to be read" and the stream stays dead. Capture
 * therefore only ever touches RAM, and a lower-priority thread pays the
 * erase latency. Sized for roughly two seconds at 8 kHz, which covers a
 * sector erase many times over. */
#define STAGE_BYTES  8192
#define WRITER_STACK 1024

RING_BUF_DECLARE(stage, STAGE_BYTES);
static K_THREAD_STACK_DEFINE(writer_stack, WRITER_STACK);
static struct k_thread writer_thread;
static struct k_sem    writer_wake;
static uint32_t        stage_drops;
static uint32_t        n_pages, n_erases, n_wake, n_pushes, n_clears;
static atomic_t        clear_requested;

/* The flash is put to sleep when there is nothing to write and nothing left
 * to replay, which on a device streaming live is nearly all the time.
 *
 * Suspended deliberately rather than through runtime PM: this part wants
 * about 8 ms to leave deep power-down, and paying that per page write would
 * cost more throughput than the writer has to spare. Idle is the only safe
 * place to spend it. */
#define FLASH_IDLE_MS 5000
static bool    flash_suspended;
static int64_t last_io_ms;

static void flash_wake(void)
{
    last_io_ms = k_uptime_get();
#ifdef CONFIG_PM_DEVICE
    if (!flash_suspended) {
        return;
    }
    if (pm_device_action_run(flash_dev, PM_DEVICE_ACTION_RESUME) == 0) {
        flash_suspended = false;
    }
#endif
}

static void flash_maybe_sleep(void)
{
#ifdef CONFIG_PM_DEVICE
    if (flash_suspended || (w_pos - r_pos) > 0 || !ring_buf_is_empty(&stage)) {
        return;
    }
    if (k_uptime_get() - last_io_ms < FLASH_IDLE_MS) {
        return;
    }
    if (pm_device_action_run(flash_dev, PM_DEVICE_ACTION_SUSPEND) == 0) {
        flash_suspended = true;
    }
#endif
}

static void writer_fn(void *a, void *b, void *cc);
static void do_clear(void);

/* Called by the writer as it works, not only when it goes back to sleep.
 *
 * Reporting once per outer iteration was not enough: draining a large backlog
 * keeps this thread inside its inner loops for as long as there is work, and
 * a thread that is alive and busy looked exactly like one that had wedged.
 * The watchdog reset the board for it -- correctly, on the evidence it had.
 * The evidence was wrong. */
static void (*alive_cb)(void);
void qspi_store_set_alive_cb(void (*cb)(void)) { alive_cb = cb; }
static qspi_drain_fn drain_cb;
static qspi_ready_fn ready_cb;

/* Reads `len` bytes from a monotonic position, splitting the request when it
 * crosses the end of the device.
 *
 * The payload read handled the wrap and the two-byte header read did not, so
 * once the reader reached the last byte of flash every header read failed
 * ("read error: address or size exceeds expected values. Addr: 0x1fffff
 * size 2") and the drain stopped for good: the backlog sat at nearly a
 * megabyte and the device buffered everything from then on. */
static int read_wrapped(int64_t pos, uint8_t *dst, uint32_t len);

static uint32_t addr_of(int64_t pos)
{
    return capacity ? (uint32_t)(pos % capacity) : 0;
}

static int read_wrapped(int64_t pos, uint8_t *dst, uint32_t len)
{
    flash_wake();
    uint32_t at    = addr_of(pos);
    uint32_t first = capacity - at;

    if (first >= len) {
        return flash_read(flash_dev, at, dst, len);
    }
    int err = flash_read(flash_dev, at, dst, first);
    if (err) {
        return err;
    }
    return flash_read(flash_dev, 0, dst + first, len - first);
}

/* Erase ahead of the write pointer, one sector at a time.
 *
 * Erasing the sector at `erased_pos` destroys whatever the flash currently
 * holds at those physical addresses, which is the logical range starting a
 * whole capacity earlier. If the reader has not passed that yet, it is about
 * to read bytes that no longer exist, so it is pushed forward a sector and
 * the loss is counted. Dropping whole sectors rather than single records is
 * what makes the magic byte necessary on the way out.
 */
static int erase_ahead(void)
{
    flash_wake();
    while (w_pos + PAGE > erased_pos) {
        if (alive_cb) {
            alive_cb();      /* a sector erase alone can run to 100 ms */
        }
        int err = flash_erase(flash_dev, addr_of(erased_pos), SECTOR);
        if (err) {
            LOG_ERR("erase at 0x%x failed (%d)", addr_of(erased_pos), err);
            return err;
        }
        int64_t destroyed_through = erased_pos - (int64_t)capacity + SECTOR;
        if (r_pos < destroyed_through) {
            r_pos = destroyed_through;
            dropped++;
        }
        n_erases++;
        erased_pos += SECTOR;
    }
    return 0;
}

static int flush_page(void)
{
    if (page_fill == 0) {
        return 0;
    }
    /* Pad to a full page: NOR programs whole pages, and 0xFF leaves the tail
     * erased so a later record can still be told apart from written data. */
    memset(page_buf + page_fill, 0xFF, PAGE - page_fill);

    int err = erase_ahead();
    if (err) {
        return err;
    }
    flash_wake();
    err = flash_write(flash_dev, addr_of(w_pos), page_buf, PAGE);
    if (err) {
        LOG_ERR("write at 0x%x failed (%d)", addr_of(w_pos), err);
        return err;
    }
    n_pages++;
    w_pos += PAGE;
    page_fill = 0;
    return 0;
}

int qspi_store_init(void)
{
    k_mutex_init(&lock);

    flash_dev = DEVICE_DT_GET(DT_NODELABEL(p25q16h));
    if (!device_is_ready(flash_dev)) {
        LOG_WRN("QSPI flash not ready");
        return -ENODEV;
    }

    const struct flash_parameters *fp = flash_get_parameters(flash_dev);
    if (fp == NULL) {
        LOG_WRN("no flash parameters");
        return -ENODEV;
    }
    /* size is in bits in the devicetree; the driver reports bytes. */
    struct flash_pages_info info;
    if (flash_get_page_info_by_offs(flash_dev, 0, &info) != 0) {
        LOG_WRN("no page info");
        return -ENODEV;
    }
    capacity = (uint32_t)flash_get_page_count(flash_dev) * info.size;
    if (capacity == 0) {
        return -ENODEV;
    }

    w_pos = r_pos = erased_pos = 0;
    dropped = page_fill = stage_drops = 0;
    ring_buf_reset(&stage);
    k_sem_init(&writer_wake, 0, 1);
    ready = true;

    k_thread_create(&writer_thread, writer_stack, WRITER_STACK,
                    writer_fn, NULL, NULL, NULL,
                    K_PRIO_PREEMPT(12), 0, K_NO_WAIT);
    k_thread_name_set(&writer_thread, "qspi");
    LOG_INF("QSPI ready: %u KB, %u B sectors", capacity / 1024, info.size);

    return 0;
}

bool     qspi_store_ready(void)    { return ready; }
/* Telemetry, read without locking from whichever thread asks. The values can
 * be a few frames stale against each other, which is fine for a counter on a
 * screen and is not used to decide anything. */
uint32_t qspi_store_pending(void)
{
    return (uint32_t)(w_pos - r_pos) + ring_buf_size_get(&stage);
}
uint32_t qspi_store_capacity(void) { return capacity; }

void qspi_store_set_drain(qspi_drain_fn fn, qspi_ready_fn ready)
{
    drain_cb = fn;
    ready_cb = ready;
}

void qspi_store_stats(uint32_t out[4])
{
    out[0] = n_pushes; out[1] = n_pages; out[2] = n_erases; out[3] = n_wake;
}
uint32_t qspi_store_dropped(void)  { return dropped + stage_drops; }

/* Thread contract for the staging ring.
 *
 * Zephyr's ring_buf is safe for one producer and one consumer without a lock,
 * and that is exactly what this is: the capture thread is the only caller of
 * qspi_store_push, and the writer thread is the only one that takes bytes out
 * -- including the drain path in qspi_store_pop, which runs on the writer.
 * The space check followed by two puts is safe for the same reason: only the
 * producer consumes space, so a check that passes cannot be invalidated by
 * the consumer, which only frees space.
 *
 * A lock here would be actively harmful. The writer holds the store mutex
 * across sector erases that take upwards of a hundred milliseconds, and
 * blocking the capture thread on that is what overran the microphone and
 * killed the PDM stream once already. The capture thread must never wait on
 * flash. Documented rather than locked, deliberately.
 */
int qspi_store_push(const uint8_t *data, uint8_t len)
{
    if (!ready || len == 0 || len > QSPI_MAX_PAYLOAD) {
        return -EINVAL;
    }
    uint8_t hdr[2] = { QSPI_MAGIC, len };

    /* All or nothing: a header without its payload would desynchronise the
     * reader far worse than dropping the frame outright. */
    if (ring_buf_space_get(&stage) < (uint32_t)(sizeof(hdr) + len)) {
        stage_drops++;
        return -ENOSPC;
    }
    n_pushes++;
    ring_buf_put(&stage, hdr, sizeof(hdr));
    ring_buf_put(&stage, data, len);
    k_sem_give(&writer_wake);
    return 0;
}

/* Moves staged bytes to flash. Runs below the capture thread so the erase it
 * blocks on never delays a microphone read. */
static void writer_fn(void *a, void *b, void *cc)
{
    ARG_UNUSED(a); ARG_UNUSED(b); ARG_UNUSED(cc);

    for (;;) {
        k_sem_take(&writer_wake, K_MSEC(20));
        n_wake++;
        if (atomic_cas(&clear_requested, 1, 0)) {
            do_clear();
        }
        if (alive_cb) {
            alive_cb();
        }

        /* Replay to the host before anything else: the backlog is older
         * audio and has to reach the host ahead of what is being captured
         * now, or the two get spliced together. */
        while (drain_cb != NULL && ready_cb != NULL && ready_cb() &&
               (w_pos - r_pos) > 0) {
            if (alive_cb) {
                alive_cb();          /* still working, not stuck */
            }
            uint8_t rec[QSPI_MAX_PAYLOAD];
            int n = qspi_store_peek(rec, sizeof(rec));
            if (n <= 0) {
                break;
            }
            if (!drain_cb(rec, (uint16_t)n)) {
                /* Left in place. The radio was busy, not the record bad;
                 * the next pass will offer the same frame again. */
                break;
            }
            qspi_store_commit((uint8_t)n);
        }

        while (!ring_buf_is_empty(&stage)) {
            if (alive_cb) {
                alive_cb();
            }
            k_mutex_lock(&lock, K_FOREVER);

            uint32_t room = PAGE - page_fill;
            uint32_t got  = ring_buf_get(&stage, page_buf + page_fill, room);

            page_fill += got;
            if (page_fill == PAGE) {
                (void)flush_page();
            }
            k_mutex_unlock(&lock);

            if (got == 0) {
                break;
            }
        }

        flash_maybe_sleep();
    }
}

void qspi_store_commit(uint8_t len)
{
    k_mutex_lock(&lock, K_FOREVER);
    if (peeked_len && len == peeked_len) {
        r_pos += 2 + peeked_len;
        peeked_len = 0;
    }
    k_mutex_unlock(&lock);
}

int qspi_store_peek(uint8_t *out, uint8_t max_len)
{
    return qspi_store_pop(out, max_len);
}

int qspi_store_pop(uint8_t *out, uint8_t max_len)
{
    if (!ready) {
        return 0;
    }
    k_mutex_lock(&lock, K_FOREVER);

    /* Anything still staged has to reach flash before it can be read back
     * through the same addresses. */
    if (!ring_buf_is_empty(&stage)) {
        uint32_t room = PAGE - page_fill;
        page_fill += ring_buf_get(&stage, page_buf + page_fill, room);
    }
    (void)flush_page();

    int result = 0;

    /* Resynchronise: a dropped sector can leave the reader mid-record. */
    while (r_pos + 2 <= w_pos) {
        uint8_t hdr[2];

        if (read_wrapped(r_pos, hdr, sizeof(hdr)) != 0) {
            break;
        }
        if (hdr[0] != QSPI_MAGIC || hdr[1] == 0 || hdr[1] > QSPI_MAX_PAYLOAD) {
            r_pos++;                       /* scan for the next magic byte */
            continue;
        }
        uint8_t len = hdr[1];
        if (r_pos + 2 + len > w_pos) {     /* record not fully written yet */
            break;
        }
        if (len > max_len) {               /* caller's buffer is too small */
            break;
        }
        if (read_wrapped(r_pos + 2, out, len) != 0) {
            break;
        }
        /* Deliberately does NOT advance r_pos. The caller commits once the
         * frame has actually left the device. */
        peeked_len = len;
        result = len;
        break;
    }

    /* A page is padded to its end with 0xFF, and the scan eats those a byte
     * at a time -- but it needs two bytes to read a header, so a single
     * trailing pad byte can never be consumed and the backlog reads as 1 B
     * forever instead of empty. A lone byte that is not a magic marker is
     * padding by definition. */
    if (result == 0 && (w_pos - r_pos) == 1) {
        uint8_t b;
        if (read_wrapped(r_pos, &b, 1) == 0 && b != QSPI_MAGIC) {
            r_pos = w_pos;
        }
    }

    k_mutex_unlock(&lock);
    return result;
}

void qspi_store_reset(void)
{
    if (!ready) {
        return;
    }
    /* Asked for here, done by the writer.
     *
     * Resetting the cursors alone left whatever was already queued in the
     * staging ring to be written straight back into the buffer that had just
     * been cleared, so "discard buffer" did not discard it. The ring has one
     * consumer by contract and clearing it from the Bluetooth thread would
     * break that, so the request is flagged and the writer performs it. */
    atomic_set(&clear_requested, 1);
    k_sem_give(&writer_wake);
}

static void do_clear(void)
{
    k_mutex_lock(&lock, K_FOREVER);
    ring_buf_reset(&stage);          /* consumer side: this is the writer */
    w_pos = r_pos = erased_pos = 0;
    page_fill = 0;
    peeked_len = 0;
    n_clears++;
    k_mutex_unlock(&lock);
    LOG_INF("backlog cleared");
}
