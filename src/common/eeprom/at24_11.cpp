#include <algorithm>

#include "at24_11.hpp"

static const uint8_t AT24_PAGE = 8;  // AT24C02-compatible (8-byte pages)

bool at24_write(uint8_t dev, uint8_t word, const uint8_t* d, uint8_t n) {
  while (n) {
    const uint8_t pageOff = word % AT24_PAGE;
    const uint8_t chunk = static_cast<uint8_t>(std::min<uint8_t>(AT24_PAGE - pageOff, n));

    Wire.beginTransmission(dev);
    Wire.write(word);
    Wire.write(d, chunk);
    if (Wire.endTransmission() != 0) return false;

    // ACK polling (tWR ~5ms typ, guard at ~20ms)
    uint32_t t0 = millis();
    while (true) {
      Wire.beginTransmission(dev);
      if (Wire.endTransmission() == 0) break; // device responded
      if (millis() - t0 > 20) return false;   // timeout guard
      delay(1);
    }

    word += chunk; d += chunk; n -= chunk;
  }
  return true;
}

bool at24_read( uint8_t dev, uint8_t word, uint8_t* d, uint8_t n) {
  Wire.beginTransmission(dev);
  Wire.write(word);
  if (Wire.endTransmission(false) != 0) return false; // repeated start
  if (Wire.requestFrom(dev, n) != n) return false;
  for (uint8_t i=0; i<n; i++) d[i] = Wire.read();
  return true;
}
