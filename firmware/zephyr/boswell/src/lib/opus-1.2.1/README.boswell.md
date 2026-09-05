# Opus 1.2.1 (vendored)

Not a dependency this project resolves — a copy, checked in.

Opus is not part of nRF Connect SDK, and there is no Zephyr module for it,
so the alternatives were to vendor it or to use LC3 (which *is* in NCS).
Opus won on the host side: `libopus.so.0` is already installed on the
machine that decodes these recordings, whereas liblc3 would have had to be
built first. On the device the two are comparable at 32 kbps.

This copy came from Omi's firmware
(`omi/firmware/devkit/src/lib/opus-1.2.1`), which matters because it is not
stock upstream Opus: it is flattened into one directory, carries the ARM
assembly under `arm/`, and is configured `FIXED_POINT` with
`DISABLE_FLOAT_API` — the shape that actually fits on an nRF52840 and has
been run there in production at the bitrate this project wants.

Changes made on the way in:

- `CmakeLists.txt` renamed to `CMakeLists.txt`, so `add_subdirectory` finds
  it. As shipped, the file was never picked up by anything; Omi lists all
  130 sources in their application CMakeLists instead. The file was already
  a correct `zephyr_library_named(opus_codec)` and needed nothing else.
- `COPYING` added. Every source file still carries its own BSD-3-Clause
  notice, but the top-level licence had been dropped, and a vendored
  library should carry the terms it is used under.

Upstream: https://gitlab.xiph.org/xiph/opus (tag v1.2.1)
