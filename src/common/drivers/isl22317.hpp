#pragma once

#include <Arduino.h>
#include <Wire.h>

#ifndef ISL22317_ADDR
#define ISL22317_ADDR 0x28   // adjust if strapped differently
#endif

// Set volatile wiper (0..127) on the currently selected mux channel
bool isl22317_set_wiper_on_selected(uint8_t wiper);

// Read current volatile wiper (0..127) on the selected mux channel
bool isl22317_read_wiper_on_selected(uint8_t &wiper_out);