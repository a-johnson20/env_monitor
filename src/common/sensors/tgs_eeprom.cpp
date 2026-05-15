#include "common/sensors/tgs_eeprom.hpp"
#include "common/eeprom/at24_11.hpp"
#include "hal/i2c_addresses.hpp"
#include <string.h>  // memcpy

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

// EEPROM layout for R2ppm (bytes 0x03..0x07):
//   0x03..0x06 — float (4 bytes, native/little-endian on ESP32)
//   0x07       — XOR CRC of bytes 0x03..0x06
bool tgs_read_r2ppm_on_selected(float &out_r2ppm_kohm, bool &crc_ok) {
  uint8_t buf[5] = {0};
  if (!at24_read(hal::I2CAddr::AT24, 0x03, buf, 5)) return false;
  crc_ok = (crc8_xor(buf, 4) == buf[4]);
  memcpy(&out_r2ppm_kohm, buf, 4);
  return true;
}

bool tgs_write_r2ppm_on_selected(float r2ppm_kohm) {
  uint8_t payload[5] = {0};
  memcpy(payload, &r2ppm_kohm, 4);
  payload[4] = crc8_xor(payload, 4);
  return at24_write(hal::I2CAddr::AT24, 0x03, payload, 5);
}