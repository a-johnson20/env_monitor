#pragma once
#include <Arduino.h>
#include <Wire.h>

#include "hal/mux_map.hpp"

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
  enum class Page : uint8_t { CO2, RH, T, P, V2611, V2616, Count };

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

  // --- Adaptive per-channel sparklines for TGS, CO2 & TRHP ---
  // Push one sample into the per-physical-slot history
  inline void pushSample2611(uint8_t ch, float v, bool fresh) {
    if (ch < TGS2611_SLOTS_) sparks_2611_[ch].push(v, fresh);
  }
  inline void pushSample2616(uint8_t ch, float v, bool fresh) {
    if (ch < TGS2616_SLOTS_) sparks_2616_[ch].push(v, fresh);
  }
  inline void pushSampleCO2(uint8_t ch, float v, bool fresh) {
    if (ch < CO2_SLOTS_) sparks_co2_[ch].push(v, fresh);
  }
  inline void pushSampleTRHP_RH(uint8_t ch, float v, bool fresh) {
    if (ch < TRHP_SLOTS_) sparks_rh_[ch].push(v, fresh);
  }
  inline void pushSampleTRHP_T(uint8_t ch, float v, bool fresh) {
    if (ch < TRHP_SLOTS_) sparks_t_[ch].push(v, fresh);
  }
  inline void pushSampleTRHP_P(uint8_t ch, float v, bool fresh) {
    if (ch < TRHP_SLOTS_) sparks_p_[ch].push(v, fresh);
  }
  // Tell the UI which physical slot is currently displayed as #k
  inline void setV2611Phys(uint8_t ch) { v2611_phys_ = (ch < TGS2611_SLOTS_) ? ch : 0; }
  inline void setV2616Phys(uint8_t ch) { v2616_phys_ = (ch < TGS2616_SLOTS_) ? ch : 0; }
  inline void setCO2Phys(uint8_t ch)  { co2_phys_  = (ch < CO2_SLOTS_)  ? ch : 0; }
  inline void setTRHPPhys(uint8_t ch) { trhp_phys_ = (ch < TRHP_SLOTS_) ? ch : 0; }

  // Expose the current graph sample period so main.cpp can push at the same cadence.
  inline unsigned long graphSamplePeriodMs() const { return GRAPH_SAMPLE_MS_; }

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
  Page page_ = Page::CO2;
  bool autorotate_ = false;
  unsigned long page_period_ms_ = 2000;

  // Spark timing
  static constexpr unsigned long GRAPH_SPAN_MS_ = 15UL * 60UL * 1000UL; // 15 min
  unsigned long GRAPH_SAMPLE_MS_ = 7000; // computed from W_ in begin()

  // One spark per signal (shared signals)
  Spark sparks_[static_cast<size_t>(Signal::COUNT)];

  // Per-physical-slot spark histories for adaptive identical sensors.
  // Use at least size 1 to keep arrays valid when no slots are compiled.
  static constexpr size_t TGS2611_SLOTS_ = hal::Mux::TGS2611.size() ? hal::Mux::TGS2611.size() : 1;
  static constexpr size_t TGS2616_SLOTS_ = hal::Mux::TGS2616.size() ? hal::Mux::TGS2616.size() : 1;
  static constexpr size_t CO2_SLOTS_  = (hal::Mux::SCD4x.size() > 0 ? hal::Mux::SCD4x.size() : 1);
  static constexpr size_t TRHP_SLOTS_ = (hal::Mux::TRHP.size()  > 0 ? hal::Mux::TRHP.size()  : 1);
  Spark sparks_2611_[TGS2611_SLOTS_];
  Spark sparks_2616_[TGS2616_SLOTS_];
  Spark  sparks_co2_[CO2_SLOTS_];
  Spark  sparks_rh_[TRHP_SLOTS_];
  Spark  sparks_t_[TRHP_SLOTS_];
  Spark  sparks_p_[TRHP_SLOTS_];
  uint8_t v2611_phys_ = 0;
  uint8_t v2616_phys_ = 0;
  uint8_t co2_phys_  = 0;
  uint8_t trhp_phys_ = 0;
};

} // namespace ui
