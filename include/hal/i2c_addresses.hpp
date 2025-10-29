#pragma once

#include <cstdint>

namespace hal::I2CAddr {
  inline constexpr uint8_t AT24     = 0x50;
  inline constexpr uint8_t ISL22317 = 0x28; // app-only
  inline constexpr uint8_t TCA9548A = 0x70;
  inline constexpr uint8_t SSD1306 = 0x3D;
  inline constexpr uint8_t ADS1113 = 0x48;
  inline constexpr uint8_t LPS22DF = 0x5D;
  inline constexpr uint8_t SCD41 = 0x62;
}