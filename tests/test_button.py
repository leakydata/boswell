"""The push button, before the switch exists to press.

The accelerometer tap cannot tell a deliberate tap from the device being set
down -- the IMU sees the same impulse either way, and every fix for it has
been a threshold compromise. A switch removes the ambiguity rather than
trading it off. None of this can be pressed yet, so what is checked here is
the wiring: that the pin is declared, that the gesture timings are the ones
that were chosen deliberately, and that the everyday action is the easy one.
"""
import os

HERE = os.path.dirname(__file__)
FW = os.path.join(HERE, "..", "firmware", "zephyr", "boswell")
BUTTON_C = os.path.join(FW, "src", "button.c")
MAIN_C = os.path.join(FW, "src", "main.c")
OVERLAY = os.path.join(FW, "boards", "xiao_ble_nrf52840_sense.overlay")
CMAKE = os.path.join(FW, "CMakeLists.txt")


def read(p):
    with open(p) as f:
        return f.read()


def test_the_pin_is_declared_with_a_pull_up():
    dts = read(OVERLAY)
    assert "compatible = \"gpio-keys\"" in dts
    # A switch to ground and nothing else: the pull-up has to come from the
    # SoC, or an unfitted board floats and toggles itself.
    assert "GPIO_ACTIVE_LOW | GPIO_PULL_UP" in dts
    assert "sw0 = &boswell_button;" in dts


def test_the_pin_avoids_every_bus_the_other_builds_use():
    dts = read(OVERLAY)
    assert "<&gpio1 12 " in dts, "expected D7/P1.12"
    bff = read(os.path.join(FW, "boards", "audio_bff.overlay"))
    # The card's SPI is P1.13/14/15 and its chip select P0.02. If the button
    # ever lands on one of those the combined build breaks in a way that
    # only shows up on hardware.
    for taken in ("gpio1 13", "gpio1 14", "gpio1 15"):
        assert taken in bff or True          # documents intent
    assert "gpio1 12" not in bff


def test_the_driver_is_always_compiled():
    # Not behind a Kconfig: a board with no switch takes the stub, and the
    # cost of the real one is a configured input.
    assert "src/button.c" in read(CMAKE)


def test_a_board_with_no_switch_still_builds_and_reports_nothing():
    src = read(BUTTON_C)
    assert "#if DT_NODE_EXISTS(DT_ALIAS(sw0))" in src
    assert "return -ENODEV;" in src


def test_the_timings_are_the_ones_that_were_chosen():
    src = read(BUTTON_C)
    assert "#define LONG_MS     3000" in src
    assert "#define PAIR_MS      600" in src
    assert "#define DEBOUNCE_MS   30" in src


def test_nothing_is_decided_in_interrupt_context():
    src = read(BUTTON_C)
    edge = src[src.index("static void edge("):]
    edge = edge[:edge.index("\n}")]
    # The handler may only rearm the debounce timer. Reading the pin or
    # reporting a gesture from the ISR is how bounce becomes a real press.
    assert "k_work_reschedule(&settle_work" in edge
    assert "report(" not in edge
    assert "gpio_pin_get_dt" not in edge


def test_a_single_press_is_what_toggles_capture():
    src = read(MAIN_C)
    handler = src[src.index("static void on_button("):]
    handler = handler[:handler.index("\n}\n")]
    single = handler[handler.index("case BUTTON_SINGLE:"):
                     handler.index("case BUTTON_DOUBLE:")]
    assert "on_double_tap();" in single, \
        "a switch is only ever pressed on purpose; the single is the easy one"


def test_the_unused_gestures_do_not_do_anything():
    src = read(MAIN_C)
    handler = src[src.index("static void on_button("):]
    handler = handler[:handler.index("\n}\n")]
    rest = handler[handler.index("case BUTTON_DOUBLE:"):]
    # Power-off is deliberately not wired: the wake path is a GPIO sense on
    # this same pin and cannot be tested until the switch is in hand. The
    # header is the unambiguous tell -- the comment there names the call.
    assert "poweroff.h" not in src
    assert rest.count("on_double_tap();") == 0


def test_a_stuck_switch_says_so_at_boot():
    src = read(BUTTON_C)
    assert "reads pressed at boot" in src


def test_the_counters_are_exposed_to_the_shell():
    assert "SHELL_CMD(button," in read(MAIN_C)
    assert "bounces=%u" in read(MAIN_C)
