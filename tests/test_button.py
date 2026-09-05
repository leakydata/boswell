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


def test_the_pin_is_declared_the_way_the_board_is_wired():
    dts = read(OVERLAY)
    assert "compatible = \"gpio-keys\"" in dts
    # Not a switch to ground. This board bridges two pins -- D4 held high as
    # the supply, D5 sensing -- so a press sources 3.3 V rather than pulling
    # anything down. Measured on the board: pull-down gave 15 transitions,
    # pull-up gave none at all, because it swamps the signal.
    assert "GPIO_ACTIVE_HIGH | GPIO_PULL_DOWN" in dts
    assert "sw0 = &boswell_button;" in dts


def test_the_supply_pin_exists():
    # Nothing is readable until D4 is driven. A sense pin alone reads a quiet
    # line however hard the button is pressed, which is what five empty scans
    # looked like before the schematic turned up.
    dts = read(OVERLAY)
    assert "btn_supply" in dts
    assert "<&gpio0 4 GPIO_ACTIVE_HIGH>" in dts


def test_the_bus_that_owns_the_button_pins_is_disabled():
    # A peripheral left status = "okay" that nothing uses still owns its
    # pins, and the generated devicetree is the only place that says so.
    # i2c1's pinctrl is exactly P0.04 and P0.05 -- the button's two pins.
    import re
    dts = read(OVERLAY)
    m = re.search(r"&i2c1\s*\{(.*?)\};", dts, re.S)
    assert m, "&i2c1 is not overridden at all"
    assert 'status = "disabled"' in m.group(1), "&i2c1 still owns D4 and D5"


def test_the_pins_do_not_collide_with_the_card():
    dts = read(OVERLAY)
    assert "<&gpio0 5 " in dts, "sense on D5/P0.05"
    bff = read(os.path.join(FW, "boards", "audio_bff.overlay"))
    # The card's SPI is P1.13/14/15 with chip select P0.02. The button is on
    # P0.04 and P0.05, and the speaker on P0.03/28/29 -- no overlap.
    assert "gpio0 5 " not in bff
    assert "gpio0 4 " not in bff


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


def test_double_is_still_bound_to_nothing_and_long_powers_off():
    src = read(MAIN_C)
    handler = src[src.index("static void on_button("):]
    handler = handler[:handler.index("\n}\n")]
    rest = handler[handler.index("case BUTTON_DOUBLE:"):]
    # Long press powers the device off, now that there is a real switch to
    # prove the gesture against. Double is still bound to nothing.
    dbl = rest[rest.index("case BUTTON_DOUBLE:"):rest.index("case BUTTON_LONG:")]
    assert dbl.count("on_double_tap();") == 0
    assert "power_off();" in rest


def test_a_stuck_switch_says_so_at_boot():
    src = read(BUTTON_C)
    assert "reads pressed at boot" in src


def test_the_counters_are_exposed_to_the_shell():
    assert "SHELL_CMD(button," in read(MAIN_C)
    assert "bounces=%u" in read(MAIN_C)
