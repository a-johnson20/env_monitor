#pragma once
#include <Arduino.h>

// Project includes
#include "../sensors/tgs_eeprom.hpp"
#include "../drivers/isl22317.hpp"

// Look up ID in table; set digipot; log to Serial.
// Returns true if a wiper was written (table match or default path).
bool calibrate_tgs_on_selected(const CalEntry* tab, size_t n,
                               const char* label, uint8_t default_wiper);
