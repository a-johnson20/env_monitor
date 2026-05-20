#include "common/drivers/ws2812_leds.hpp"
#include <FastLED.h>
#include <Arduino.h>

#define LED_PIN  40
#define NUM_LEDS  9

static CRGB     s_leds[NUM_LEDS];
static uint32_t s_led_off_ms[NUM_LEDS] = {};  // non-zero: turn that LED off at this millis()

namespace leds {

void begin() {
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(s_leds, NUM_LEDS);
  FastLED.setBrightness(50);

  // Startup wipe: white comet sweeping LED1 → LED9
  // Centre LED = full white; falloff ±1 = dim, ±2 = very dim
  for (int i = 0; i < NUM_LEDS; i++) {
    for (int j = 0; j < NUM_LEDS; j++) {
      int     d = abs(j - i);
      uint8_t b = (d == 0) ? 255 : (d == 1) ? 100 : (d == 2) ? 30 : 0;
      s_leds[j] = CRGB(b, b, b);
    }
    FastLED.show();
    delay(100);
  }
  FastLED.clear();
  FastLED.show();
}

void poll(uint32_t now_ms) {
  bool changed = false;
  for (int i = 0; i < NUM_LEDS; i++) {
    if (s_led_off_ms[i] && now_ms >= s_led_off_ms[i]) {
      s_leds[i]      = CRGB::Black;
      s_led_off_ms[i] = 0;
      changed         = true;
    }
  }
  if (changed) FastLED.show();
}

void led_flash(uint8_t idx, bool success, uint32_t ms) {
  if (idx >= NUM_LEDS) return;
  s_leds[idx]      = success ? CRGB::Green : CRGB::Red;
  FastLED.show();
  s_led_off_ms[idx] = millis() + ms;
}

void led1_flash(bool success, uint32_t ms) { led_flash(0, success, ms); }

} // namespace leds
