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
  src/qspi_store.c
)

# Only built for a board that has a card. See boards/audio_bff.overlay.
if(CONFIG_DISK_DRIVER_SDMMC)
  target_sources(app PRIVATE src/sd_probe.c)
endif()
target_include_directories(app PRIVATE src)
