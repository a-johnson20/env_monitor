#pragma once
#include <array>
#include <cstdint>

namespace Mux {
  enum class Ch : uint8_t { C0, C1, C2, C3, C4, C5, C6, C7 };

  // <Ch, 2> means "channel Ch, two devices expected"
  // Define channels { Ch::C5, Ch::C3 } means "channel C5 and C3, one device each"
  inline constexpr std::array<Ch, 1> TRHP    { Ch::C0 }; // SHT45 + TMP117 + LPS22DF
  inline constexpr std::array<Ch, 1> SCD4x   { Ch::C1 }; // SCD41 / SCD40
  inline constexpr std::array<Ch, 2> TGS2611 { Ch::C3, Ch::C4 }; // TGS2611 with ADS1113 + (future) digipot/EEPROM
  inline constexpr std::array<Ch, 1> TGS2616 { Ch::C5 }; // TGS2616 with ADS1113 + (future) digipot/EEPROM
}
