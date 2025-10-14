#include "at24_ll.h"

static const uint8_t AT24_PAGE = 8;  // AT24C02-compatible (8-byte pages)

bool at24_write(uint8_t dev, uint8_t word, const uint8_t* d, uint8_t n){
  while (n) {
    uint8_t pageOff = word % AT24_PAGE;
    uint8_t chunk   = (uint8_t)min<int>(AT24_PAGE - pageOff, n);

    Wire.beginTransmission(dev);
    Wire.write(word);
    Wire.write(d, chunk);
    if (Wire.endTransmission() != 0) return false;

    // ACK polling (tWR ~5ms)
    uint32_t t0 = millis();
    while (millis() - t0 < 20) {
      Wire.beginTransmission(dev);
      if (Wire.endTransmission() == 0) break;
      delay(1);
    }

    word += chunk; d += chunk; n -= chunk;
  }
  return true;
}

bool at24_read(uint8_t dev, uint8_t word, uint8_t* d, uint8_t n){
  Wire.beginTransmission(dev);
  Wire.write(word);
  if (Wire.endTransmission(false) != 0) return false; // repeated start
  if (Wire.requestFrom(dev, n) != n) return false;
  for (uint8_t i=0; i<n; i++) d[i] = Wire.read();
  return true;
}