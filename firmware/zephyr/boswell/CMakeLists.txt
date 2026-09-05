cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(boswell)

target_sources(app PRIVATE
  src/main.c
  src/mic.c
  src/codec.c
  src/ble_audio.c
  src/imu_tap.c
  src/battery.c
  src/led.c
  src/cfg_store.c
  src/fault.c
  src/qspi_store.c
)

# Only built for a board that has a card. See boards/audio_bff.overlay.
if(CONFIG_DISK_DRIVER_SDMMC)
  target_sources(app PRIVATE src/sd_probe.c)
endif()
# Vendored, not fetched -- see src/lib/opus-1.2.1/README.boswell.md for why
# a copy and not a module, and what was changed on the way in.
if(CONFIG_BOSWELL_OPUS)
  add_subdirectory(src/lib/opus-1.2.1)
  target_include_directories(app PRIVATE src/lib/opus-1.2.1)
  # Named explicitly. zephyr_library_named() registers opus_codec for the
  # final image, but app is itself a static library and the linker sees it
  # before opus -- so the codec's symbols are already past by the time
  # anything asks for them, and every call comes back undefined.
  target_link_libraries(app PRIVATE opus_codec)
endif()

target_include_directories(app PRIVATE src)
