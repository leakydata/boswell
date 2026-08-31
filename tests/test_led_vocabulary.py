"""The status light means one thing, in four places, and nothing kept them so.

The colours are defined in the Zephyr firmware, again in the Arduino firmware,
again in the device page, and again in the README. Four hand-written copies of
one fact, and the fact is read off a device in a room where none of the other
three are visible -- so a copy that has drifted is not a documentation problem,
it is somebody misreading their own hardware.

It has already happened. Zephyr used magenta for replaying a backlog while the
Arduino build used magenta for recording into flash and cyan for replaying:
the same colour on the same hardware meaning two different things depending on
which firmware happened to be on it. Nobody noticed until the light was read
carefully for another reason.

So the copies are compared here rather than trusted. This does not test the
firmware -- it tests that the four descriptions of it agree.
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

ZEPHYR_LED_H = os.path.join(ROOT, "firmware/zephyr/boswell/src/led.h")
ZEPHYR_MAIN = os.path.join(ROOT, "firmware/zephyr/boswell/src/main.c")
ARDUINO = os.path.join(ROOT, "firmware/ble_mic/ble_mic.ino")
PAGE = os.path.join(ROOT, "web/static/index.html")
README = os.path.join(ROOT, "README.md")

# The vocabulary itself. Adding a state means adding it here, which is the
# point: the test then demands it in all four places.
STATES = {
    "LED_RECORDING":    ("green",   (False, True,  False)),
    "LED_CATCHING_UP":  ("cyan",    (False, True,  True)),
    "LED_BUFFERING":    ("magenta", (True,  False, True)),
    "LED_IDLE_LINKED":  ("red",     (True,  False, False)),
    "LED_IDLE_WAITING": ("blue",    (False, False, True)),
    "LED_NO_FLASH":     ("yellow",  (True,  True,  False)),
}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _bools(*words):
    return tuple(w.strip() == "true" for w in words)


def zephyr_colours():
    out = {}
    for m in re.finditer(r"#define\s+(LED_[A-Z_]+)\s+"
                         r"(true|false),\s*(true|false),\s*(true|false)",
                         read(ZEPHYR_LED_H)):
        out[m.group(1)] = _bools(m.group(2), m.group(3), m.group(4))
    return out


def arduino_colours():
    out = {}
    for m in re.finditer(r"static const LedColour\s+(LED_[A-Z_]+)\s*=\s*\{\s*"
                         r"(true|false),\s*(true|false),\s*(true|false),\s*"
                         r'"([a-z]+)"', read(ARDUINO)):
        out[m.group(1)] = (_bools(m.group(2), m.group(3), m.group(4)), m.group(5))
    return out


def test_every_state_is_defined_in_the_running_firmware():
    have = zephyr_colours()
    for name in STATES:
        assert name in have, f"{name} is not defined in led.h"


def test_the_two_firmwares_light_the_same_channels():
    """The same hardware must not mean two things depending on the build."""
    z, a = zephyr_colours(), arduino_colours()
    for name, (word, channels) in STATES.items():
        assert z[name] == channels, f"{name}: led.h has {z[name]}, expected {channels}"
        assert a[name][0] == channels, f"{name}: the Arduino build disagrees"
        assert a[name][1] == word, f"{name}: the Arduino build calls it {a[name][1]}"


def test_no_two_states_share_a_colour():
    """A colour that means two things cannot be read off the device at all,
    which is the exact fault this vocabulary was written to fix."""
    z = zephyr_colours()
    seen = {}
    for name in STATES:
        assert z[name] not in seen, f"{name} and {seen[z[name]]} are the same colour"
        seen[z[name]] = name


def test_the_running_firmware_uses_every_state():
    """A state nobody sets is a colour documented and never shown."""
    src = read(ZEPHYR_MAIN)
    for name in STATES:
        assert f"setLed({name})" in src or f"led_set_colour({name})" in src, \
            f"{name} is defined but never used"


def test_the_device_page_lists_them_all():
    page = read(PAGE)
    key = page[page.index('class="ledkey"'):]
    key = key[:key.index("</div>\n      <label")] if "</div>\n      <label" in key else key[:4000]
    for word in (w for w, _ in STATES.values()):
        assert f"<b>{word}</b>" in key, f"the device page does not explain {word}"


def test_the_readme_lists_them_all():
    doc = read(README)
    for word in (w for w, _ in STATES.values()):
        assert re.search(rf"^\|\s*{word}\s*\|", doc, re.M), \
            f"the README does not explain {word}"


def test_the_page_paints_a_colour_with_one_hex_everywhere():
    """The key and the live status pill describe the same states, so the same
    word must not be one green in the legend and a different green above it."""
    page = read(PAGE)
    for word, hexes in {
        "green": "#5ad07a", "cyan": "#4ad0d0", "red": "#e05a5a",
        "blue": "#4a90e0", "magenta": "#c060d0", "yellow": "#e0c04a",
    }.items():
        found = set(re.findall(rf"{hexes}", page, re.I))
        assert found, f"{word} ({hexes}) is not used anywhere on the page"
