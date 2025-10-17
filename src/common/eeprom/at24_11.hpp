#pragma once
#include <Arduino.h>
#include <Wire.h>

// Simple XOR CRC used for tiny on-module headers
inline uint8_t crc8_xor(const uint8_t* p, 
                        uint8_t n) {
  uint8_t c = 0; for (uint8_t i=0;i<n;i++) c ^= p[i]; return c;
}

// Low-level AT24 helpers (handle page-splitting + ACK polling)
bool at24_write(uint8_t dev, uint8_t word, const uint8_t* d, uint8_t n);
bool at24_read (uint8_t dev, uint8_t word, uint8_t* d, uint8_t n);