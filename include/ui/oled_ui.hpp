#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

namespace ui {

// A minimal "view-model" that the OLED renderer needs.
// Fill this from your app code each time you call update(...).
struct Model {
  // Clock string like "2025-10-29 14:37:05"
  const char* clock_text = nullptr;

  // Sensor presence flags
  bool scd4x_present = false;
  bool trhp_present  = false;
  bool tgs2611_present = false;
  bool tgs2616_present = false;

  // Latest readings (set any you use; others can be left at defaults)
  uint16_t co2_ppm = 0;
  bool     co2_fresh = false;

  float sht45_rh = NAN;
  bool  sht45_rh_fresh = false;

  float tmp117_t = NAN;
  bool  tmp117_t_fresh = false;

  float lps22df_p = NAN;
  bool  lps22df_p_fresh = false;

  float tgs2611_v = NAN;
  bool  tgs2611_v_fresh = false;

  float tgs2616_v = NAN;
  bool  tgs2616_v_fresh = false;
};

// Encapsulates the SSD1306 and all drawing/screen-rotation logic.
// Manages its own 15-minute sparkline histories for each metric you feed it.
class OledUi {
public:
  OledUi();
  ~OledUi() = default;

  // Initialize the OLED. Returns true if the panel ACKs on the bus.
  bool begin(TwoWire& wire, uint8_t i2c_addr = 0x3D, uint16_t width = 128, uint16_t height = 64);

  // Update the screen at most every `min_period_ms`. Safe to call each loop.
  void update(const Model& m, unsigned long min_period_ms = 2000);

  // Manually clear and splash a short message (e.g., during boot).
  void splash(const __FlashStringHelper* line1, const __FlashStringHelper* line2 = nullptr);

  // Accessor to know if an OLED was detected.
  bool present() const { return present_; }

private:
  void drawHeader_(const Model& m);
  void drawGraph_(float value, bool fresh, const char* label);
  void pushSample_(float value, bool fresh, uint8_t which);

private:
  // Simple circular buffers for 15-minute sparklines (width samples)
  struct Spark {
    float v[128];
    uint8_t ok[128];
    uint16_t head = 127;
    bool filled = false;
    void push(uint16_t W, float x, bool good) {
      head = (head + 1) % W;
      v[head] = x; ok[head] = good ? 1 : 0;
      if (head == W-1) filled = true;
    }
  };

  enum Metric : uint8_t { M_CO2, M_RH, M_T, M_P, M_V2611, M_V2616, M_COUNT };

  Adafruit_SSD1306* disp_ = nullptr;
  uint16_t W_ = 128, H_ = 64;
  bool present_ = false;
  unsigned long last_update_ms_ = 0;
  unsigned long last_sample_ms_ = 0;
  static constexpr unsigned long GRAPH_SPAN_MS_   = 15UL * 60UL * 1000UL; // 15 min
  unsigned long GRAPH_SAMPLE_MS_ = 7000; // computed from W_
  Spark sparks_[M_COUNT];
};

} // namespace ui
