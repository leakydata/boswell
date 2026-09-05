"""Audio onto the card, as files.

The 2 MB QSPI ring holds 4.4 minutes -- enough to walk out of Bluetooth range
and come back, not enough to spend a day away from the computer, which is the
point of fitting a card. These cover the decisions that are easy to get
subtly wrong: when audio spills to the card, what the file says about itself,
and what happens when the card is not there.
"""
import os
import re

HERE = os.path.dirname(__file__)
FW = os.path.join(HERE, "..", "firmware", "zephyr", "boswell")
SD_C = os.path.join(FW, "src", "sd_store.c")
SD_H = os.path.join(FW, "src", "sd_store.h")
MAIN_C = os.path.join(FW, "src", "main.c")
CMAKE = os.path.join(FW, "CMakeLists.txt")


def read(p):
    with open(p) as f:
        return f.read()


def body(src, sig):
    i = src.index(sig)
    return src[i:src.index("\n}\n", i)]


def test_the_radio_is_preferred_over_the_card():
    # Audio the host takes live becomes a conversation now; audio on the card
    # waits for a cable. Only what the radio cannot take should spill.
    fn = body(read(MAIN_C), "static int drain_record(const uint8_t *rec, uint16_t len)")
    assert fn.index("ble_audio_ready()") < fn.index("sd_store_write")


def test_the_ring_is_not_bypassed():
    # The obvious version -- radio if connected, card if not -- puts audio on
    # the card the moment you walk into the next room, turning a gap that used
    # to heal itself on reconnect into one that needs a dock.
    fn = body(read(MAIN_C), "static int drain_record(const uint8_t *rec, uint16_t len)")
    assert "ring_needs_spilling()" in fn, \
        "the card is the overflow behind the ring, not an alternative to it"


def test_the_spill_starts_before_the_ring_is_full():
    src = read(MAIN_C)
    pct = int(re.search(r"#define SPILL_AT_PERCENT (\d+)", src).group(1))
    assert 50 <= pct <= 90, \
        "too low wastes the live-replay window; too high leaves no room to " \
        "capture into while the card stalls"


def test_popping_is_only_offered_when_something_can_take_it():
    # Popping advances the read pointer, so a record taken out while nothing
    # can accept it is simply lost.
    fn = body(read(MAIN_C), "static bool drain_ready(void)")
    assert "ble_audio_ready()" in fn and "ring_needs_spilling()" in fn


def test_the_file_says_what_is_in_it():
    src = read(SD_C)
    hdr = body(src, "static int open_next(void)")
    for field in ("SD_FILE_MAGIC", "SD_FILE_VERSION", "fmt_codec", "fmt_rate",
                  "fmt_boot"):
        assert field in hdr, f"{field} missing from the file header"


def test_the_boot_id_reaches_the_header():
    # The host keys de-duplication on (boot_id, device_ms). Without boot_id a
    # docked file cannot be placed against audio that already arrived live.
    assert "ble_audio_boot_id()" in read(MAIN_C)


def test_a_format_change_starts_a_new_file():
    # One file, one header. Switching rate mid-file would leave the host
    # decoding the back half with the front half's settings.
    src = read(SD_C)
    assert "fmt_changed" in body(src, "void sd_store_format(uint8_t codec")
    assert "fmt_changed" in body(src, "int sd_store_write(const uint8_t *rec")


def test_the_writer_never_blocks_forever_on_the_card():
    # The first mount of this card took sixteen seconds. A writer that waits
    # that long is a writer that has stopped feeding its watchdog.
    src = read(SD_C)
    fn = body(src, "int sd_store_write(const uint8_t *rec")
    assert "K_MSEC(LOCK_MS)" in fn
    assert "K_FOREVER" not in fn


def test_a_busy_card_is_not_a_lost_record():
    src = read(SD_C)
    fn = body(src, "int sd_store_write(const uint8_t *rec")
    # Returning 0 keeps it in the ring to be offered again. Returning -1
    # would tell the ring to drop it.
    lock = fn.index("K_MSEC(LOCK_MS)")
    after = fn[lock:lock + 400]
    assert "return 0;" in after


def test_a_failed_write_closes_the_file():
    # A short write leaves a torn record at the end. Appending past it buries
    # the damage inside a file the host reads straight through.
    fn = body(read(SD_C), "static int flush_batch(void)")
    bad = fn[fn.index("if (w != (ssize_t)batch_len)"):]
    assert "fs_close" in bad and "file_open = false" in bad


def test_the_data_is_committed_not_just_written():
    # Without fs_sync a file written but never closed has zero length after a
    # power loss -- the data sits in clusters nothing points at.
    assert "fs_sync" in body(read(SD_C), "static int flush_batch(void)")


def test_stopping_capture_flushes_what_is_in_ram():
    src = read(MAIN_C)
    # Every path that stops capture, not just the tap.
    assert src.count("sd_store_flush();") >= 3, \
        "the tap, the shell and the app all stop capture"


def test_a_board_with_no_card_slot_still_builds():
    h = read(SD_H)
    assert "#ifdef CONFIG_DISK_DRIVER_SDMMC" in h
    assert "static inline int  sd_store_write" in h
    # and the stub must say "not now", not "never" -- "never" would tell the
    # ring to discard the backlog it exists to protect.
    stub = h[h.index("static inline int  sd_store_write"):]
    stub = stub[:stub.index("}")]
    assert "return 0;" in stub


def test_the_store_is_only_compiled_where_there_is_a_card():
    assert "src/sd_store.c" in read(CMAKE)
    line = [l for l in read(CMAKE).splitlines() if "sd_store.c" in l][0]
    assert "sd_probe.c" in line, "guarded by the same CONFIG_DISK_DRIVER_SDMMC block"


def test_the_files_can_be_listed_and_checked():
    assert "SHELL_CMD(cardls," in read(MAIN_C)
    fn = body(read(SD_C), "int sd_store_list(const struct shell *sh)")
    assert "SD_FILE_MAGIC" in fn and "bad magic" in fn
    # listing has to include what is still in RAM or the newest file reads short
    assert "flush_batch()" in fn
