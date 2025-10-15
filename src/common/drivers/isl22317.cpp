// Project includes
#include "isl22317.hpp"

bool isl22317_set_wiper_on_selected(uint8_t wiper) {
  if (wiper > 127) wiper = 127;
  Wire.beginTransmission(ISL22317_ADDR);
  Wire.write(0x00);          // WCR register
  Wire.write(wiper);
  return Wire.endTransmission() == 0;
}
