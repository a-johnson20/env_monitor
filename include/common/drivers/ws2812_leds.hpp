#pragma once
#include <cstdint>

namespace leds {

/// Initialise FastLED and run the startup wipe animation.
/// Call once from setup(), after Serial is open.
void begin();

/// Non-blocking poll — call every loop() iteration.
/// Turns LED1 off once its flash timer has expired.
void poll(uint32_t now_ms);

/// Flash any LED by index (0 = LED1 … 8 = LED9) for `ms` milliseconds.
/// success = true → green, false → red.  Index is bounds-checked silently.
void led_flash(uint8_t idx, bool success, uint32_t ms = 500);

/// Convenience: flash LED1 (LoRa status, index 0).
void led1_flash(bool success, uint32_t ms = 500);

} // namespace leds
