#include "isl22317.hpp"

bool isl22317_set_wiper_on_selected(uint8_t wiper) {
  if (wiper > 127) wiper = 127;
  Wire.beginTransmission(ISL22317_ADDR);
  Wire.write(0x00);          // WCR register
  Wire.write(wiper);
  return Wire.endTransmission() == 0;
}

bool isl22317_read_wiper_on_selected(uint8_t &wiper_out) {
  // Point to WCR (0x00), then repeated start and read 1 byte
  Wire.beginTransmission(ISL22317_ADDR);
  Wire.write(0x00);
  if (Wire.endTransmission(false) != 0) return false;   // repeated start

  if (Wire.requestFrom(ISL22317_ADDR, 1) != 1) return false;
  wiper_out = Wire.read() & 0x7F; // 0..127
  return true;
}