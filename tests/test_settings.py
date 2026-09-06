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
