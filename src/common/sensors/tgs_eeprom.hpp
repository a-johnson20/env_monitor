#pragma once
#include <Arduino.h>
#include <Wire.h>

// Project includes
#include "config.hpp"

// Reads the 16-bit Sensor ID + checks 1-byte XOR CRC from addresses 0x00..0x02.
// Returns true if I2C read succeeded; sets crc_ok accordingly.
bool tgs_read_sensor_id_on_selected(uint16_t &out_id, bool &crc_ok);
