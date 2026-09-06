"""The settings page, and the recorders it lists.

The Device tab answers "is it working right now". This one answers "what is
it, and how is it set up" -- the distinction that stopped the Device tab from
becoming twenty controls of wildly different importance in one column.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "web"))


def read(p):
    with open(os.path.join(HERE, "..", p)) as f:
        return f.read()


def test_the_tab_exists_and_is_wired_everywhere():
    # Two places enumerate the tabs, and a tab added to one and not the other
    # shows a panel that never hides or hides one that never shows.
    html = read("web/static/index.html")
    assert 'id="tabSet"' in html and 'id="viewSet"' in html
    assert '["tabSet","viewSet","set"]' in html
    assert '["tabSet","set"]' in html


def test_recorders_are_counted_from_the_archive():
    # Not from a list somebody maintains. The archive is the only thing that
    # actually knows which devices have fed it.
    src = read("web/server.py")
    fn = src[src.index("async def api_devices("):]
    fn = fn[:fn.index("\n@app.")]
    assert "os.listdir(TIMES)" in fn
    assert "device_id" in fn


def test_a_device_that_stopped_being_used_still_appears():
    # With its last clip, which is the question worth answering about one.
    src = read("web/server.py")
    fn = src[src.index("async def api_devices("):]
    fn = fn[:fn.index("\n@app.")]
    assert '"last"' in fn
    assert "connected" in fn


def test_unattributed_audio_is_not_counted_as_a_recorder():
    # Everything recorded before clips carried a device id is one bucket, not
    # one device. Saying "3 recorders" when there are two is the kind of
    # small lie a settings page exists not to tell.
    html = read("web/static/index.html")
    fn = html[html.index("async function loadSettings("):]
    fn = fn[:fn.index("\nfunction st_ok(")]
    assert "filter(x => x.device_id)" in fn
    assert "recorded before" in fn


def test_the_settings_page_does_not_start_or_stop_anything():
    # A settings page that can silently stop a recorder is one that
    # eventually does. It reports; systemctl acts.
    html = read("web/static/index.html")
    fn = html[html.index("async function loadSettings("):]
    fn = fn[:fn.index("\nfunction st_ok(")]
    assert "method: \"POST\"" not in fn and "method:\"POST\"" not in fn


def test_it_loads_on_open_rather_than_polling():
    # None of it changes second to second, and a settings page that repaints
    # under you is hard to read.
    html = read("web/static/index.html")
    assert 'if (which === "set") loadSettings();' in html
    assert "setInterval(loadSettings" not in html


# ------------------------------------------------- how the archive knows
#
# A recorder that stamped its own name into a file is not the same kind of
# fact as somebody concluding afterwards which device it must have been. An
# archive that cannot tell those apart has quietly lost the ability to say
# how it knows anything.

def test_an_inferred_attribution_is_marked_as_such():
    src = read("host/backfill_device.py")
    assert 'rec["device_id_inferred"] = True' in src


def test_the_originals_are_copied_before_they_are_rewritten():
    # Three thousand small rewrites is exactly the shape of operation that is
    # fine until it is not, and the times records are the only account of
    # when this audio happened.
    src = read("host/backfill_device.py")
    fn = src[src.index("def main("):]
    assert "shutil.copytree" in fn
    assert fn.index("copytree") < fn.index('rec["device_id"] = args.device')


def test_the_backfill_does_nothing_unless_asked():
    src = read("host/backfill_device.py")
    assert '"--write"' in src
    assert "dry run" in src


def test_a_named_clip_is_never_overwritten():
    # Only clips with no recorder at all are touched. A device that said who
    # it was outranks anything concluded later.
    src = read("host/backfill_device.py")
    assert 'if rec.get("device_id"):' in src


def test_the_count_reaches_the_interface():
    assert '"inferred"' in read("web/server.py")
    html = read("web/static/index.html")
    assert "attributed `" in html and "not recorded at the time" in html


# ------------------------------------------------ clips nobody queued
#
# The live path queues its own clips as it finalises them, which was enough
# while every clip came from this process. It stopped being enough the moment
# a second recorder appeared: omid is a separate program writing into the same
# archive, with no worker to submit to. 665 Omi clips sat indexed, searchable
# by name and silent -- never transcribed, never sound-tagged.

def test_a_sweep_queues_whatever_has_no_transcript():
    src = read("web/server.py")
    assert "async def transcriber_sweep(" in src
    fn = src[src.index("async def transcriber_sweep("):]
    fn = fn[:fn.index("\n@asynccontextmanager")]
    assert "pipeline.transcript_path(f)" in fn
    assert "worker.submit(f)" in fn


def test_the_sweep_is_actually_started():
    # A background task defined and never created is worse than none: it
    # reads as solved.
    assert "asyncio.create_task(transcriber_sweep())" in read("web/server.py")


def test_the_sweep_yields_to_the_live_transcriber():
    src = read("web/server.py")
    fn = src[src.index("async def transcriber_sweep("):]
    fn = fn[:fn.index("\n@asynccontextmanager")]
    assert "worker.busy" in fn
    assert "worker.q.qsize()" in fn


def test_the_sweep_is_bounded_per_pass():
    # One sweep must not fill the queue with a thousand clips nobody can see
    # the end of.
    src = read("web/server.py")
    fn = src[src.index("async def transcriber_sweep("):]
    fn = fn[:fn.index("\n@asynccontextmanager")]
    assert "queued >= 8" in fn


def test_the_backlog_comes_in_the_order_it_happened():
    src = read("web/server.py")
    fn = src[src.index("async def transcriber_sweep("):]
    fn = fn[:fn.index("\n@asynccontextmanager")]
    assert "sorted(os.listdir(DATA))" in fn
