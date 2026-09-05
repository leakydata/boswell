"""Browsing and collecting recordings over the radio.

The cable is twenty times faster and already works, so this is a convenience
-- it is for wanting one conversation from this morning while the device is
on your chest. What matters is that the convenient path cannot corrupt the
archive the careful path fills.
"""
import os
import re

HERE = os.path.dirname(__file__)
FW = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src")
PROTO = os.path.join(FW, "proto.h")
XFER = os.path.join(FW, "sd_xfer.c")
BLE = os.path.join(FW, "ble_audio.c")
SERVER = os.path.join(HERE, "..", "web", "server.py")
INDEX = os.path.join(HERE, "..", "web", "static", "index.html")


def read(p):
    with open(p) as f:
        return f.read()


def body(src, sig):
    i = src.index(sig)
    return src[i:src.index("\n}\n", i)]


def test_the_host_and_firmware_agree_on_the_commands():
    proto, server = read(PROTO), read(SERVER)
    assert "FILES_LIST   = 0x01" in proto
    assert "FILES_READ   = 0x02" in proto
    assert "bytes([0x01])" in server               # list
    assert "bytes([0x02, index & 0xFF" in server   # read


def test_the_uuid_matches_on_both_sides():
    fw = re.search(r"BOSWELL_UUID_FILES\s*\\\n\s*BT_UUID_128_ENCODE\(0x(\w+),",
                   read(PROTO))
    assert fw and fw.group(1) == "4b1a0006"
    assert "4b1a0006-8f2c-4d5e-9a3b-1c7e6f8d0a21" in read(SERVER)


def test_nothing_slow_runs_on_the_bluetooth_thread():
    # That thread carries the live audio's acknowledgements. A directory walk
    # or a megabyte read there stops the recording the device exists for.
    fn = body(read(BLE), "static ssize_t files_write(")
    assert "sd_xfer_command" in fn
    for slow in ("fs_opendir", "fs_read", "fs_open"):
        assert slow not in fn


def test_the_card_lock_is_not_held_across_the_whole_file():
    # A megabyte at this rate is four minutes. Holding the card that long
    # would stop the recording still going on behind the transfer.
    fn = body(read(XFER), "static void do_read(uint16_t which)")
    assert fn.count("k_mutex_lock(&sd_lock") >= 2, \
        "expected the lock to be taken per chunk, not once"
    assert "k_msleep" in fn, "the transfer has to yield to live audio"


def test_a_usb_host_owning_the_card_refuses_the_transfer():
    # Reading a volume somebody else is editing.
    fn = body(read(XFER), "static void xfer_fn(")
    assert "sd_is_released()" in fn
    assert fn.index("sd_is_released()") < fn.index("case FILES_LIST")


def test_unsubscribing_abandons_the_transfer():
    # Somebody closed the page. Reading the card for nobody costs power and
    # stalls the writer.
    fn = body(read(BLE), "static void files_ccc_changed(")
    assert "sd_xfer_abort()" in fn


def test_a_gap_in_the_chunks_is_fatal_rather_than_papered_over():
    # A hole in a .bwl is a torn record the reader stops at anyway, and a
    # short file returned as complete would be ingested as complete.
    src = read(SERVER)
    fn = src[src.index("async def pull_card_file"):]
    fn = fn[:fn.index("\n    async def ")]
    assert "len(chunks) != expected" in fn
    assert "return None" in fn


def test_the_pulled_file_goes_through_the_same_ingest_as_the_cable():
    # Two ways in, one set of rules about what is trustworthy. A second
    # ingest path would be a second place for de-duplication to be wrong.
    src = read(SERVER)
    fn = src[src.index("async def api_device_files_pull"):]
    fn = fn[:fn.index("\n@app.")]
    assert "ingest_card.ingest_file" in fn
    assert "ingest_card.save_ledger" in fn


def test_the_listing_says_what_is_already_held():
    src = read(SERVER)
    fn = src[src.index("async def api_device_files("):]
    fn = fn[:fn.index("\n@app.")]
    # Same key the cable path writes, so a file collected either way is only
    # ever ingested once.
    assert "f\"{f['name']}:{f['size']}\" in ledger" in fn


def test_the_radio_is_asked_rather_than_polled():
    # Asking over the radio costs a round trip and a directory walk on a card
    # that is still being written to. The docked case is polled, because
    # plugging a cable in is the event and it costs only a host-side listing.
    html = read(INDEX)
    assert 'if (recRoute !== "ble") recPoll(0);' in html, \
        "the radio route must not be on the timer"
    assert '$("recbtn").onclick = () => recPoll(1);' in html


def test_there_is_one_control_not_two():
    # Two importing-shaped buttons a few rows apart, one of them usually
    # invisible, and the invisible one is the one that gets clicked.
    html = read(INDEX)
    for gone in ("cardimport", "devfilesbtn", "devfilesrow", "cardrow"):
        assert gone not in html, f"{gone} should have been merged away"
    assert 'id="recrow"' in html


def test_both_routes_end_at_the_same_ingest():
    # The whole reason for merging: two controls were two chances to build
    # two sets of rules about what is trustworthy.
    src = read(SERVER)
    fn = src[src.index("async def api_recordings_import"):]
    fn = fn[:fn.index("\n@app.")]
    assert "ingest_card.ingest_file" in fn      # the docked route
    assert "api_device_files_pull_inner" in fn  # the radio route


def test_the_subscription_is_dropped_when_the_link_is():
    # A subscription belongs to a connection, not to the Device object. Left
    # set across a reconnect it claims a handler the new link does not have,
    # the device answers into nothing, and the timeout reads as an empty
    # card -- which is how a working listing started reporting "empty" for a
    # card holding two recordings.
    src = read(SERVER)
    fn = src[src.index("async def _session(self):"):]
    fn = fn[:fn.index("\n    async def ", 10)]
    assert "self._files_sub = False" in fn


def test_a_timeout_is_not_reported_as_an_empty_card():
    src = read(SERVER)
    fn = src[src.index("async def list_card_files"):]
    fn = fn[:fn.index("\n    async def ")]
    assert "finished" in fn
    assert "if not finished:" in fn
    assert "return None" in fn


def test_the_ui_distinguishes_no_answer_from_nothing_there():
    html = read(INDEX)
    assert "no answer from the device" in html
    assert "nothing on the card" in html
    assert "nothing recorded yet" in html


def test_a_press_during_a_poll_is_not_thrown_away():
    # Dropping it silently is indistinguishable from a broken button, which
    # is the complaint this section exists to answer.
    html = read(INDEX)
    assert "recAskAgain" in html
    fn = html[html.index("async function recPoll(ask){"):]
    fn = fn[:fn.index("\n}")]
    assert "if (ask) recAskAgain = true;" in fn
