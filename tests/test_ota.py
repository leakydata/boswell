"""Updating the device without finding the USB cable.

CTRL_DFU (0x0F) and INFO_CAP_OTA (0x0008) have been in the firmware since the
beginning and the host never sent them, so updating something meant to be
worn always meant a cable -- and, when the shell did not answer, a blind
double-tap on a reset button.
"""
import os
import re

HERE = os.path.dirname(__file__)
SERVER = os.path.join(HERE, "..", "web", "server.py")
INDEX = os.path.join(HERE, "..", "web", "static", "index.html")
PROTO = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src", "proto.h")
MAIN_C = os.path.join(HERE, "..", "firmware", "zephyr", "boswell", "src", "main.c")


def read(p):
    with open(p) as f:
        return f.read()


def test_the_host_sends_the_opcode_the_firmware_defines():
    assert "CTRL_DFU          = 0x0F," in read(PROTO)
    assert "self._ctrl(0x0F, arg)" in read(SERVER)


def test_the_arguments_are_the_awkward_ones_the_firmware_demands():
    # 0x5A and 0xA5 are deliberate: the firmware ignores anything else so a
    # stray control write cannot take the device offline.
    fw = read(MAIN_C)
    assert "arg == 0x5A" in fw and "arg == 0xA5" in fw
    srv = read(SERVER)
    assert "arg = 0xA5 if over_air else 0x5A" in srv


def test_the_capability_bit_is_parsed_and_published():
    srv = read(SERVER)
    assert "has_ota = bool(caps & 0x0008)" in srv
    assert 'out["has_ota"] = has_ota' in srv


def test_over_the_air_is_refused_when_the_firmware_cannot_do_it():
    srv = read(SERVER)
    fn = srv[srv.index("async def enter_dfu"):]
    fn = fn[:fn.index("\n    async def ")]
    assert 'if over_air and not self.state.get("has_ota")' in fn
    # and it must bail before writing anything
    assert fn.index("has_ota") < fn.index("self._ctrl(0x0F")


def test_the_reconnect_loop_is_stopped_afterwards():
    srv = read(SERVER)
    fn = srv[srv.index("async def enter_dfu"):]
    fn = fn[:fn.index("\n    async def ")]
    # The device comes back as a bootloader, which does not advertise the
    # audio service. Chasing it forever is noise.
    assert "self.want(False)" in fn


def test_the_websocket_accepts_it():
    assert 'elif cmd == "dfu":' in read(SERVER)
    assert 'device.enter_dfu(bool(msg.get("over_air", False)))' in read(SERVER)


def test_the_button_needs_two_taps():
    html = read(INDEX)
    handler = html[html.index('$("dfubtn").onclick'):]
    handler = handler[:handler.index("\n};")]
    assert "armDfu" in handler
    assert 'send({cmd:"dfu"' in handler
    # the first tap must not send anything
    first = handler[:handler.index("return;")]
    assert "send(" not in first


def test_the_control_is_hidden_when_the_firmware_cannot_do_it():
    html = read(INDEX)
    assert '$("dfurow").hidden = !s.has_ota;' in html
    assert '$("dfubtn").disabled = !s.has_ota || !s.connected;' in html
