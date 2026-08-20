#include "qspi_store.h"
#include "cfg_store.h"
#include "rec_crc.h"

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
/* Why a read of the backlog came back empty. Every exit from the read loop
 * used to be a bare break, so a store holding audio it could not read looked
 * exactly like an empty one. */
static uint32_t pop_fail_read, pop_fail_short, pop_fail_big, pop_scanned;
static uint32_t pop_ok, drain_refused, drain_skipped, drain_stuck_drops;
static uint32_t pop_crc_fail;
/* Writes and erases the flash rejected. Silence here reads as an idle store. */
static uint32_t write_fails, erase_fails;
static int      last_write_err;
static uint32_t stuck_offers;
/* About ten seconds of retries at the writer's 20 ms tick. */
#define STUCK_OFFER_LIMIT 500
static int      pop_last_err;

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
 * megabyte and the device buffered everything from then on.
 *
 * Exercised at full capacity on 2026-08-20, which had never been done: the
 * host was dropped and capture ran into flash at 8.5 KB/s until the erase
 * count passed 1026 -- two complete laps of the 2 MB ring -- and every frame
 * was then validated on the way out, checking header fields, the ADPCM step
 * index range, the sample count and the payload length against it.
 *
 *     32,038 frames (5381 KB), 17,207 of them replayed from flash
 *     malformed frames: 0
 *
 * The 150 frames the device reported dropped are the writer lapping the
 * reader with nothing draining, which is the designed behaviour. */
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

/* Commit the staged page.
 *
 * A failure here leaves page_fill at PAGE, which makes room zero on the next
 * pass, so the writer stops taking bytes out of the staging ring rather than
 * overwriting a page it never wrote -- and retries this on the following
 * wake. That much was already right, by construction rather than by
 * intention.
 *
 * What was missing is that it said nothing. The return value was discarded at
 * both call sites, so a flash that had stopped accepting writes looked like a
 * quiet one: the staging ring filled, frames were dropped at the far end as
 * stage_drops, and the only number that moved was one whose name gives no
 * hint that the flash is the reason.
 */
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
        if (erase_fails++ == 0) {
            LOG_ERR("erase ahead of 0x%x failed (%d); the backlog is stalling",
                    addr_of(w_pos), err);
        }
        last_write_err = err;
        return err;
    }
    flash_wake();
    err = flash_write(flash_dev, addr_of(w_pos), page_buf, PAGE);
    if (err) {
        if (write_fails++ == 0) {
            LOG_ERR("write at 0x%x failed (%d); the backlog is stalling",
                    addr_of(w_pos), err);
        }
        last_write_err = err;
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

    /* Pick up where the last boot left off, if the flash agrees.
     *
     * The audio itself survives a reset -- it is in external flash -- but
     * these cursors did not, so every watchdog reset, crash or battery
     * interruption discarded the whole backlog. Store-and-forward exists so
     * that being out of range costs latency rather than audio, and losing it
     * to a reboot is the same loss by another route.
     *
     * Trusted only after the flash is checked against them: a record must
     * actually start at the saved read cursor. If anything disagrees the
     * store comes up empty, which is exactly the old behaviour, so a bad
     * record degrades to what this already did rather than to garbage. */
    struct boswell_backlog saved;

    if (cfg_store_load_backlog(&saved)) {
        int64_t pending = saved.w_pos - saved.r_pos;

        if (pending > 0 && pending <= (int64_t)capacity &&
            saved.r_pos >= 0 && saved.w_pos >= saved.r_pos) {
            /* One matching byte is not evidence.
             *
             * This checked only that a 0xB5 sat at the saved read cursor,
             * which any byte has a one-in-256 chance of being. It passed on a
             * cursor pointing into the middle of a record, the length byte
             * that followed was 1, and drain_to_host refuses anything that
             * small -- so 163 KB of audio sat unread and the device buffered
             * continuously with nothing saying why.
             *
             * Parse a whole record instead, and require the next one to begin
             * with a magic byte as well. Two records agreeing is a cursor
             * that means something. */
            uint8_t hdr[QSPI_HDR_LEN] = { 0 };
            bool sane = false;

            if (flash_read(flash_dev, (uint32_t)(saved.r_pos % capacity),
                           hdr, sizeof(hdr)) == 0 &&
                hdr[0] == QSPI_MAGIC && hdr[1] > 2 &&
                hdr[1] <= QSPI_MAX_PAYLOAD) {
                int64_t next = saved.r_pos + QSPI_HDR_LEN + hdr[1];

                if (next + QSPI_HDR_LEN <= saved.w_pos) {
                    uint8_t nmagic = 0;

                    sane = flash_read(flash_dev,
                                      (uint32_t)(next % capacity),
                                      &nmagic, 1) == 0 &&
                           nmagic == QSPI_MAGIC;
                } else {
                    sane = true;       /* single record; nothing after it */
                }
            }
            /* And the bytes must still be the ones that were there when the
             * cursor was written. */
            if (sane) {
                uint8_t fp[8] = { 0 };

                if ((saved.w_pos - saved.r_pos) >= (int64_t)sizeof(fp) &&
                    flash_read(flash_dev,
                               (uint32_t)(saved.r_pos % capacity),
                               fp, sizeof(fp)) == 0) {
                    if (memcmp(fp, saved.fingerprint, sizeof(fp)) != 0) {
                        LOG_WRN("backlog cursor does not match the flash "
                                "underneath it; starting empty");
                        sane = false;
                    }
                }
            }
            if (sane) {
                w_pos = saved.w_pos;
                r_pos = saved.r_pos;
                erased_pos = saved.w_pos;
                LOG_INF("backlog recovered: %lld B from the previous boot",
                        (long long)pending);
            } else {
                LOG_WRN("saved backlog cursors do not match the flash; "
                        "starting empty");
            }
        }
    }

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
    /* Under the lock, because this is not only telemetry.
     *
     * The capture thread reads it every frame to decide whether a frame goes
     * to flash or to the radio, so a value assembled from a w_pos read before
     * a commit and an r_pos read after it is a routing decision made on a
     * state that never existed. The cost is a mutex on a path that already
     * takes one to push.
     */
    k_mutex_lock(&lock, K_FOREVER);
    uint32_t n = (uint32_t)(w_pos - r_pos) + ring_buf_size_get(&stage);
    k_mutex_unlock(&lock);
    return n;
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
    uint8_t hdr[QSPI_HDR_LEN] = { QSPI_MAGIC, len, rec_crc8(data, len) };

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
/* Record where the backlog has got to, so a reset does not discard it.
 *
 * Rate limited hard. This lands in the internal flash the settings live in,
 * and audio arrives at about 8.5 KB/s while disconnected -- saving on every
 * write would burn the partition for no benefit, because the value of the
 * cursors is only realised on a reboot, which is rare. Once a minute while
 * there is something buffered bounds the loss to a minute of audio.
 *
 * Both edges matter: the save when the backlog first appears means a reset
 * moments later still finds it, and the save when it empties means a reset
 * after a successful drain does not replay what the host already has.
 */
#define CURSOR_SAVE_MS 60000

static void save_cursors_now(void)
{
    uint8_t fp[8] = { 0 };

    /* Best effort: a fingerprint we could not read is stored as zeroes, and
     * a zero fingerprint simply fails to match on restore. */
    if ((w_pos - r_pos) >= (int64_t)sizeof(fp)) {
        (void)read_wrapped(r_pos, fp, sizeof(fp));
    }
    cfg_store_save_backlog(w_pos, r_pos, fp);
}

static void persist_cursors(void)
{
    static int64_t last_save_ms;
    static bool    had_backlog;

    bool has = (w_pos - r_pos) > 0;
    int64_t now = k_uptime_get();

    if (has != had_backlog) {
        had_backlog = has;
        last_save_ms = now;
        save_cursors_now();
        return;
    }
    if (has && now - last_save_ms >= CURSOR_SAVE_MS) {
        last_save_ms = now;
        save_cursors_now();
    }
}

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

        persist_cursors();

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
            /* A peek that succeeds and a drain that refuses leaves the
             * backlog exactly where it was, and until now incremented
             * nothing at all -- so a store looping here forever was
             * indistinguishable from one with nothing to do. */
            pop_ok++;
            int rc = drain_cb(rec, (uint16_t)n);

            if (rc < 0) {
                /* Never deliverable. Drop it rather than offer it again. */
                drain_skipped++;
                qspi_store_commit((uint8_t)n);
                stuck_offers = 0;
                continue;
            }
            if (rc == 0) {
                drain_refused++;
                /* The radio was busy, not the record bad; the next pass
                 * offers the same frame again.
                 *
                 * Unless it keeps happening. One record that cannot be
                 * handed over stops the whole backlog for good, and a
                 * recorder that quietly stops delivering is worse than one
                 * that loses a frame -- so past this many attempts the frame
                 * goes, and the counter says it went. */
                if (++stuck_offers >= STUCK_OFFER_LIMIT) {
                    LOG_WRN("record refused %u times; dropping it to free the backlog",
                            stuck_offers);
                    drain_stuck_drops++;
                    qspi_store_commit((uint8_t)n);
                    stuck_offers = 0;
                }
                break;
            }
            stuck_offers = 0;
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
                /* Not discarded: on failure page_fill stays full, room goes
                 * to zero, and this loop stops consuming until a later pass
                 * gets the page committed. */
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
        r_pos += QSPI_HDR_LEN + peeked_len;
        peeked_len = 0;
    }
    k_mutex_unlock(&lock);
}

void qspi_store_write_stats(uint32_t out[2], int *last_err)
{
    out[0] = write_fails; out[1] = erase_fails;
    if (last_err) {
        *last_err = last_write_err;
    }
}

void qspi_store_pop_stats(uint32_t out[9], int *last_err)
{
    out[0] = pop_fail_read; out[1] = pop_fail_short;
    out[2] = pop_fail_big;  out[3] = pop_scanned;
    out[4] = pop_ok;        out[5] = drain_refused;
    out[6] = drain_skipped; out[7] = drain_stuck_drops;
    out[8] = pop_crc_fail;
    if (last_err) {
        *last_err = pop_last_err;
    }
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
    while (r_pos + QSPI_HDR_LEN <= w_pos) {
        uint8_t hdr[QSPI_HDR_LEN];
        int rc = read_wrapped(r_pos, hdr, sizeof(hdr));

        if (rc != 0) {
            /* Every exit from this loop used to be a bare break, so a store
             * holding audio it could not read looked identical to an empty
             * one -- pending sat unchanged and nothing said why. */
            pop_fail_read++;
            pop_last_err = rc;
            break;
        }
        if (hdr[0] != QSPI_MAGIC || hdr[1] == 0 || hdr[1] > QSPI_MAX_PAYLOAD) {
            r_pos++;                       /* scan for the next magic byte */
            pop_scanned++;
            continue;
        }
        uint8_t len = hdr[1];
        if (r_pos + QSPI_HDR_LEN + len > w_pos) {  /* not fully written yet */
            pop_fail_short++;
            break;
        }
        if (len > max_len) {               /* caller's buffer is too small */
            pop_fail_big++;
            break;
        }
        rc = read_wrapped(r_pos + QSPI_HDR_LEN, out, len);
        if (rc != 0) {
            pop_fail_read++;
            pop_last_err = rc;
            break;
        }
        if (rec_crc8(out, len) != hdr[2]) {
            /* Magic and a plausible length agreed and the payload did not.
             * Almost certainly a cursor that landed mid-record; scan on
             * rather than hand the host audio assembled from the wrong
             * bytes, which it has no way to recognise as wrong. */
            pop_crc_fail++;
            r_pos++;
            pop_scanned++;
            continue;
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
