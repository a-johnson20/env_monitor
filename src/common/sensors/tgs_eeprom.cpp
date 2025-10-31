#include "common/sensors/tgs_eeprom.hpp"
#include "common/eeprom/at24_11.hpp"
#include "hal/i2c_addresses.hpp"

bool tgs_read_sensor_id_on_selected(uint16_t &out_id, bool &crc_ok) {
  uint8_t rb[3] = {0};
  if (!at24_read(hal::I2CAddr::AT24, 0x00, rb, 3)) return false;
  out_id = (uint16_t(rb[0])<<8) | rb[1];
  crc_ok = (crc8_xor(rb, 2) == rb[2]);
  return true;
}