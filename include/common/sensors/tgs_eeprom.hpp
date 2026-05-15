#pragma once

#include <Arduino.h>
#include <Wire.h>

// Reads the 16-bit Sensor ID + checks 1-byte XOR CRC from addresses 0x00..0x02.
// Returns true if I2C read succeeded; sets crc_ok accordingly.
bool tgs_read_sensor_id_on_selected(uint16_t &out_id, bool &crc_ok);

// Read the R2ppm reference resistance (kΩ) stored at EEPROM addresses 0x03..0x07.
// Layout: [float 4 bytes little-endian][XOR CRC 1 byte].
// Returns true if the I2C read succeeded (independent of CRC); sets crc_ok.
bool tgs_read_r2ppm_on_selected(float &out_r2ppm_kohm, bool &crc_ok);

// Write the R2ppm reference resistance (kΩ) to EEPROM addresses 0x03..0x07.
// Returns true if the I2C write succeeded.
bool tgs_write_r2ppm_on_selected(float r2ppm_kohm);
