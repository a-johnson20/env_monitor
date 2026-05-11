#include "common/sensors/tgs_eeprom.hpp"
#include "common/eeprom/at24_11.hpp"
#include "hal/i2c_addresses.hpp"

bool tgs_read_sensor_id_on_selected(uint16_t &out_id, bool &crc_ok) {
  uint8_t last_rb[3] = {0};
  bool had_read = false;
  for (uint8_t attempt = 0; attempt < 3; ++attempt) {
    uint8_t rb[3] = {0};
    if (!at24_read(hal::I2CAddr::AT24, 0x00, rb, 3)) continue;
    had_read = true;
    out_id = (uint16_t(rb[0])<<8) | rb[1];
    crc_ok = (crc8_xor(rb, 2) == rb[2]);
    last_rb[0] = rb[0];
    last_rb[1] = rb[1];
    last_rb[2] = rb[2];
    if (crc_ok) return true;
  }

  if (!had_read) return false;
  out_id = (uint16_t(last_rb[0])<<8) | last_rb[1];
  crc_ok = false;
  return true;
}