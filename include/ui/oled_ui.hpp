#pragma once
#include <Arduino.h>
#include <Wire.h>

class Adafruit_SSD1306;  // fwd decl; real header is included in the .cpp

namespace ui {

// ------- Public data you fill from main.cpp -------
struct Model {
  // Clock
  const char* clock_text = nullptr;

  // Latest readings + freshness
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

  // Channel index & count (for titles like “CO2 #2”)
  uint8_t co2_idx = 0,    co2_n = 1;
  uint8_t rh_idx  = 0,    rh_n  = 1;
  uint8_t t_idx   = 0,    t_n   = 1;
  uint8_t p_idx   = 0,    p_n   = 1;
  uint8_t v2611_idx = 0,  v2611_n = 1;
  uint8_t v2616_idx = 0,  v2616_n = 1;
};

// Which sparkline “signal” we store
enum class Signal : uint8_t { CO2, RH, T, P, V2611, V2616, COUNT };

class OledUi {
 public:
  // Pages we can render
  enum class Page : uint8_t { Time, CO2, RH, T, P, V2611, V2616, Count };

  OledUi();
  ~OledUi() = default;

  // Init the OLED
  bool begin(TwoWire& wire, uint8_t i2c_addr = 0x3D, uint16_t width = 128, uint16_t height = 64);

  // Boot splash
  void splash(const __FlashStringHelper* line1, const __FlashStringHelper* line2 = nullptr);

  // Draw the current page from the provided data.
  // Throttled internally by min_period_ms.
  void update(const ui::Model& m, unsigned long min_period_ms = 2000);

  // Page controls
  void setPage(Page p);
  Page currentPage() const { return page_; }
  void nextPage();

  // If you rotate in main.cpp, call setAutoRotate(false).
  void setAutoRotate(bool on, unsigned long period_ms = 2000);

  // Accessor
  bool present() const { return present_; }

 private:
  // Internal spark buffer (one per Signal)
  struct Spark {
    static constexpr uint16_t MAXW = 128;
    uint16_t W = 128;
    float    v[MAXW]{};
    uint8_t  ok[MAXW]{};
    uint16_t head  = 0;
    uint16_t count = 0;
    void configure(uint16_t width) {
      W = (width <= MAXW ? width : MAXW);
      head = (W ? (W - 1) : 0);
      count = 0;
      for (uint16_t i = 0; i < W; ++i) { v[i] = NAN; ok[i] = 0; }
    }
    void push(float x, bool good) {
      if (!W) return;
      head = (head + 1) % W;
      v[head]  = x;
      ok[head] = good ? 1 : 0;
      if (count < W) count++;
    }
    bool filled() const { return count >= W; }
  };

  // Drawing helpers
  void drawHeader_(const ui::Model& m);
  void drawGraph_(float value, bool fresh, const char* label);
  void renderPage_(const ui::Model& m);
  void pushSample_(float value, bool fresh, uint8_t which);

  // Hardware & layout
  Adafruit_SSD1306* disp_ = nullptr;
  uint16_t W_ = 128, H_ = 64;
  bool present_ = false;

  // Timing
  unsigned long last_update_ms_ = 0;
  unsigned long last_sample_ms_ = 0;
  unsigned long last_page_ms_   = 0;

  // Page rotation
  Page page_ = Page::Time;
  bool autorotate_ = false;
  unsigned long page_period_ms_ = 2000;

  // Spark timing
  static constexpr unsigned long GRAPH_SPAN_MS_ = 15UL * 60UL * 1000UL; // 15 min
  unsigned long GRAPH_SAMPLE_MS_ = 7000; // computed from W_ in begin()

  // One spark per signal
  Spark sparks_[static_cast<size_t>(Signal::COUNT)];
};

} // namespace ui
