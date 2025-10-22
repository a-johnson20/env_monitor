#pragma once

#include <Arduino.h>

// kΩ everywhere; default part is 10 kΩ
constexpr inline uint8_t wiper_code_for_kohms(float Rk_target, float RAB_kohm = 10.0f) {
  if (Rk_target <= 0.0f) return 0;
  if (Rk_target >= RAB_kohm) return 127;
  int d = static_cast<int>(127.0f * (Rk_target / RAB_kohm) + 0.5f); // round
  if (d < 0) d = 0; if (d > 127) d = 127;
  return static_cast<uint8_t>(d);
}

// NEW: Convert the volatile wiper code back to the pot resistance in kΩ.
// Note: this returns the POT value only. If you want the series "safety" 1 kΩ
// included (your comment in tgs_lookup_tables.hpp), add 1.0f.
constexpr inline float kohms_for_wiper_code(uint8_t code, float RAB_kohm = 10.0f) {
  return (static_cast<float>(code) / 127.0f) * RAB_kohm;
}

struct CalEntry;    // forward declaration from tgs_lookup_tables.hpp

// Look up ID in table; set digipot; log to Serial.
// Returns true if a wiper was written (table match or default path).
bool calibrate_tgs_on_selected( const CalEntry* tab, size_t n, const char* label, uint8_t default_wiper);
