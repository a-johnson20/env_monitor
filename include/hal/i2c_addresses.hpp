#pragma once
#include <cstdint>

namespace hal::I2CAddr {
  inline constexpr uint8_t AT24     = 0x50;
  inline constexpr uint8_t ISL22317 = 0x2C; // app-only
  inline constexpr uint8_t TCA9548A = 0x70;
}