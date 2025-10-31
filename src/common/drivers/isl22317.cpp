#include "common/drivers/isl22317.hpp"
#include "hal/i2c_addresses.hpp"

bool isl22317_set_wiper_on_selected(uint8_t wiper) {
  wiper &= 0x7F;

  Wire.beginTransmission((int)hal::I2CAddr::ISL22317); // pick int overload family
  Wire.write((uint8_t)0x00);                           // WCR
  Wire.write((uint8_t)wiper);

  // Force the bool-arg overload by calling the 1-arg (int) and passing bool explicitly
  uint8_t tx = Wire.endTransmission(true);             // STOP
  return tx == 0;
}

bool isl22317_read_wiper_on_selected(uint8_t &wiper_out) {
  delayMicroseconds(200);

  Wire.beginTransmission((int)hal::I2CAddr::ISL22317); // int,int,int family
  Wire.write((uint8_t)0x00);                           // WCR

  // Repeated-start: use the bool-arg overload explicitly via the int family
  if (Wire.endTransmission(false) != 0) return false;

  delayMicroseconds(200);

  // *** Disambiguate requestFrom *** (use the int,int,int overload)
  int got = Wire.requestFrom((int)hal::I2CAddr::ISL22317,
                             /*size*/ 1,
                             /*sendStop*/ 1);
  if (got != 1) return false;

  int b = Wire.read();
  if (b < 0) return false;

  wiper_out = (uint8_t)(b & 0x7F);
  return true;
}
