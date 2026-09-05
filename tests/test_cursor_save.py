"""Keeping the flash write off the thread the watchdog is counting on.

persist_cursors() writes the backlog cursors to internal flash. With
Bluetooth running that write has to be scheduled around the radio by MPSL,
and at a short connection interval there is very little room to grant a
timeslot -- so the write waits, with no bound on how long. While it sat on
the writer thread, that wait was inside the thread that feeds WDT_QSPI, and a
save that blocked past the thirty-second window took the board down with it.

Nothing here makes flash faster. What it checks is that the unbounded part
happens somewhere its blocking is harmless.
"""
import os
import re

HERE = os.path.dirname(__file__)
FW = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src")
QSPI_C = os.path.join(FW, "qspi_store.c")
QSPI_H = os.path.join(FW, "qspi_store.h")
MAIN_C = os.path.join(FW, "main.c")


def read(p):
    with open(p) as f:
        return f.read()


def body(src, sig):
    """The text of one function, from its signature to the closing brace."""
    i = src.index(sig)
    return src[i:src.index("\n}\n", i)]


def test_the_flash_write_is_not_on_the_writer_thread():
    src = read(QSPI_C)
    writer = body(src, "static void writer_fn(void *a, void *b, void *cc)\n{")
    assert "cfg_store_save_backlog" not in writer, \
        "the unbounded call must not be reachable directly from the writer"


def test_the_saver_thread_exists_and_does_the_write():
    src = read(QSPI_C)
    saver = body(src, "static void saver_fn(void *a, void *b, void *c)\n{")
    assert "cfg_store_save_backlog(snap.w, snap.r, snap.fp)" in saver


def test_the_saver_holds_no_lock_while_it_writes():
    src = read(QSPI_C)
    saver = body(src, "static void saver_fn(void *a, void *b, void *c)\n{")
    # The mutex must be released before the slow call. Holding it across the
    # write would block the writer on it and put the bug straight back.
    unlock = saver.index("k_mutex_unlock(&save_lock)")
    write = saver.index("cfg_store_save_backlog")
    assert unlock < write, "the lock must be dropped before touching flash"


def test_the_writer_only_ever_holds_the_lock_for_a_copy():
    src = read(QSPI_C)
    snap = body(src, "static void save_cursors_now(void)\n{")
    lock = snap.index("k_mutex_lock(&save_lock")
    unlock = snap.index("k_mutex_unlock(&save_lock)")
    between = snap[lock:unlock]
    assert "cfg_store_save_backlog" not in between
    assert "read_wrapped" not in between, \
        "the fingerprint read belongs outside the lock"


def test_the_snapshot_is_taken_where_the_cursors_live():
    # Taking it on the saver would store whatever the cursors had moved on to
    # by the time flash was free, not the state the decision was made about.
    src = read(QSPI_C)
    snap = body(src, "static void save_cursors_now(void)\n{")
    assert ".w = w_pos, .r = r_pos" in snap


def test_a_superseded_save_is_counted_rather_than_lost():
    src = read(QSPI_C)
    snap = body(src, "static void save_cursors_now(void)\n{")
    assert "save_coalesced++" in snap


def test_the_saver_is_below_the_writer_in_priority():
    src = read(QSPI_C)
    wp = int(re.search(r"k_thread_create\(&writer_thread.*?K_PRIO_PREEMPT\((\d+)\)",
                       src, re.S).group(1))
    sp = int(re.search(r"k_thread_create\(&saver_thread.*?K_PRIO_PREEMPT\((\d+)\)",
                       src, re.S).group(1))
    assert sp > wp, "bookkeeping must never be why an audio frame waits"


def test_the_saver_is_not_in_the_watchdog_mask():
    # A save blocking on the radio is the normal case this thread absorbs.
    # Requiring it to check in would reintroduce the reset it was built to
    # prevent, one thread over.
    src = read(QSPI_C)
    saver = body(src, "static void saver_fn(void *a, void *b, void *c)\n{")
    assert "alive_cb" not in saver
    assert "watchdog" not in saver.lower() or "no watchdog" in saver.lower()


def test_the_writer_still_checks_in_every_pass():
    src = read(QSPI_C)
    writer = body(src, "static void writer_fn(void *a, void *b, void *cc)\n{")
    assert "alive_cb()" in writer


def test_the_counters_reach_the_shell():
    assert "qspi_store_save_stats" in read(QSPI_H)
    assert "cursor saves=%u coalesced=%u worst=%u ms" in read(MAIN_C)


def test_both_threads_report_their_stacks():
    # The capture thread died on a margin that looked fine when measured
    # idle. These two are measured the same way, so they get reported too.
    assert "qspi_store_report_stacks" in read(MAIN_C)
    src = read(QSPI_C)
    assert '{ "qspi",      &writer_thread, WRITER_STACK }' in src
    assert '{ "qspi-save", &saver_thread,  SAVER_STACK  }' in src
