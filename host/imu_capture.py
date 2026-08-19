#!/usr/bin/env python3
"""
Record raw motion from the board to a CSV.

The device samples and the computer decides what the samples mean, the same
way the audio works. What counts as a step, or a gesture, or sitting down is
a question that will keep changing, and changing it should not mean
reflashing something somebody is wearing.

    uv run host/imu_capture.py --seconds 60 --hz 50 --out walk.csv
    uv run host/imu_capture.py --seconds 60 --gyro        # see --gyro below

The accelerometer costs roughly 10 uA. The gyroscope costs roughly 0.9 mA --
about a hundred times more -- so it is off unless asked for, and on battery
it should stay that way unless something genuinely needs rotation.
"""

import argparse
import asyncio
import struct
import sys
import time

from bleak import BleakClient, BleakScanner

DEVICE = "XIAO-MIC"
IMU_UUID = "4b1a0005-8f2c-4d5e-9a3b-1c7e6f8d0a21"
CTRL_UUID = "4b1a0003-8f2c-4d5e-9a3b-1c7e6f8d0a21"

CTRL_IMU_STREAM = 0x10
CTRL_IMU_GYRO = 0x11

HEADER = 10
FLAG_GYRO = 0x01

# +/-2 g over a signed 16-bit range, and 2000 dps for the gyroscope.
ACCEL_G = 2.0 / 32768.0
GYRO_DPS = 2000.0 / 32768.0


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--hz", type=int, default=50, help="1-255 samples per second")
    ap.add_argument("--gyro", action="store_true",
                    help="also record rotation (about 100x the current draw)")
    ap.add_argument("--out", default="imu.csv")
    ap.add_argument("--raw", action="store_true",
                    help="write ADC counts instead of g and dps")
    args = ap.parse_args()

    dev = await BleakScanner.find_device_by_name(DEVICE, timeout=20)
    if dev is None:
        raise SystemExit(f"{DEVICE} not found. Is the board powered and advertising?")
    print(f"found {dev.address}, recording {args.seconds:.0f}s at {args.hz} Hz"
          + (" with gyro" if args.gyro else ""))

    rows = []
    lost = 0
    last_seq = None

    def on_frame(_h, data):
        nonlocal lost, last_seq
        b = bytes(data)
        if len(b) < HEADER:
            return
        seq, flags, n, hz, t_ms = struct.unpack("<HBBHI", b[:HEADER])
        if last_seq is not None:
            gap = (seq - last_seq - 1) & 0xFFFF
            if gap < 1000:
                lost += gap
        last_seq = seq

        stride = 12 if (flags & FLAG_GYRO) else 6
        vals = b[HEADER:HEADER + n * stride]
        per = stride // 2
        got = struct.unpack(f"<{n * per}h", vals)
        # Every sample in a frame shares the frame's timestamp; spread them
        # across the interval they were actually taken over.
        step_ms = 1000.0 / hz if hz else 0
        for i in range(n):
            s = got[i * per:(i + 1) * per]
            rows.append((t_ms + i * step_ms, *s))

    async with BleakClient(dev, timeout=30) as c:
        await c.start_notify(IMU_UUID, on_frame)
        if args.gyro:
            await c.write_gatt_char(CTRL_UUID, bytes([CTRL_IMU_GYRO, 1]), response=True)
        await c.write_gatt_char(CTRL_UUID,
                                bytes([CTRL_IMU_STREAM, max(1, min(255, args.hz))]),
                                response=True)
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            await asyncio.sleep(0.3)
        # Leave the radio quiet and the gyroscope off behind us.
        await c.write_gatt_char(CTRL_UUID, bytes([CTRL_IMU_STREAM, 0]), response=True)
        if args.gyro:
            await c.write_gatt_char(CTRL_UUID, bytes([CTRL_IMU_GYRO, 0]), response=True)

    if not rows:
        raise SystemExit("no motion frames arrived")

    wide = len(rows[0]) == 7
    with open(args.out, "w") as f:
        if wide:
            f.write("t_ms,ax,ay,az,gx,gy,gz\n")
        else:
            f.write("t_ms,ax,ay,az\n")
        for r in rows:
            t = r[0]
            v = r[1:]
            if args.raw:
                f.write(f"{t:.1f}," + ",".join(str(x) for x in v) + "\n")
            else:
                acc = [x * ACCEL_G for x in v[:3]]
                gyr = [x * GYRO_DPS for x in v[3:]]
                f.write(f"{t:.1f}," + ",".join(f"{x:.4f}" for x in acc + gyr) + "\n")

    span = (rows[-1][0] - rows[0][0]) / 1000.0
    print(f"wrote {args.out}")
    print(f"  samples      {len(rows)} over {span:.1f}s ({len(rows)/max(span,1e-9):.0f} Hz)")
    print(f"  lost frames  {lost}")
    mag = sum((r[1] ** 2 + r[2] ** 2 + r[3] ** 2) ** 0.5 for r in rows) / len(rows)
    print(f"  mean |accel| {mag:.0f} counts ({mag * ACCEL_G:.3f} g; "
          f"1.000 g if the board is still)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
