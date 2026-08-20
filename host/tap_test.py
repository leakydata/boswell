#!/usr/bin/env python3
"""Watch capture start/stop toggling live, to verify double-tap works."""
import asyncio, os, struct, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ble_capture import AUDIO_UUID, CTRL_UUID, INFO_UUID, DEVICE_NAME, HEADER_LEN
from bleak import BleakClient, BleakScanner

async def main(seconds=45, tap_thresh=None):
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=20.0)
    if not dev:
        print("XIAO-MIC not found"); return 1
    state = {"n": 0, "last": 0.0}
    def on_audio(_s, data):
        state["n"] += 1
        state["last"] = time.time()
    async with BleakClient(dev, timeout=30.0) as c:
        info = await c.read_gatt_char(INFO_UUID)
        if len(info) >= 8:
            bus = {0:"NOT FOUND",1:"Wire1 (17/16)",2:"Wire (4/5)"}.get(info[6],"?")
            print(f"IMU: {bus}" + (f" addr=0x{info[7]:02X}" if info[6] else ""))
        if tap_thresh is not None:
            # 0x07 sets TAP_THS_6D live. Lower = more sensitive (Seeed demo uses 5).
            await c.write_gatt_char(CTRL_UUID, bytes([0x07, tap_thresh]), response=True)
            print(f"tap threshold -> {tap_thresh} (lower = more sensitive)")
        await c.start_notify(AUDIO_UUID, on_audio)
        await c.write_gatt_char(CTRL_UUID, bytes([0x01, 1]), response=True)
        print("capture STARTED via BLE. Now DOUBLE-TAP the board to toggle.\n")
        prev, t0, pdiag = None, time.time(), None
        while time.time() - t0 < seconds:
            await asyncio.sleep(0.5)
            inf = await c.read_gatt_char(INFO_UUID)
            if len(inf) >= 18:
                diag = tuple(inf[13:18])
                if diag != pdiag:
                    el = time.time() - t0
                    print(f"  [{el:5.1f}s]  TAP_IA={diag[0]} single={diag[1]} "
                          f"double={diag[2]} last_src=0x{diag[3]:02X} int1_high={diag[4]}")
                    pdiag = diag
            if len(inf) >= 26:
                import struct as _s
                # Bytes 18-21 are the layout version, firmware id and
                # capabilities. This used to decode 18-23 as six IMU
                # configuration registers and print the marker bytes as
                # register values that did not match what it expected --
                # a diagnostic confidently reporting a fault in the thing
                # it was pointed at, from bytes that were never registers.
                # The firmware no longer publishes that readback at all.
                az = _s.unpack("<h", bytes(inf[24:26]))[0]
                g = az/16384.0
                if abs(g - globals().get("_prevg", 99)) > 0.08:
                    print(f"    accel Z = {az:6d}  ({g:+.2f} g)"
                          + ("   <- accelerometer IS sampling" if az else "   <- reads zero"))
                    globals()["_prevg"] = g
            if len(inf) >= 27 and inf[26] > 2:
                pk = inf[26] * 256 / 16384.0
                el = time.time() - t0
                print(f"  [{el:5.1f}s]  SHOCK peak {pk:.2f} g  <- sensor felt that")
            flowing = (time.time() - state["last"]) < 0.6
            if flowing != prev:
                el = time.time() - t0
                print(f"  [{el:5.1f}s]  {'GREEN  capturing' if flowing else 'RED    stopped'}"
                      f"   ({state['n']} frames)")
                prev = flowing
        await c.write_gatt_char(CTRL_UUID, bytes([0x01, 0]), response=True)
    print(f"\ntotal frames: {state['n']}")
    return 0

_sec = float(sys.argv[1]) if len(sys.argv) > 1 else 45
_thr = int(sys.argv[2]) if len(sys.argv) > 2 else None
sys.exit(asyncio.run(main(_sec, _thr)))
