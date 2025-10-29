#include "ui/oled_ui.hpp"
#include <math.h>

namespace ui {

OledUi::OledUi() {}

bool OledUi::begin(TwoWire& wire, uint8_t i2c_addr, uint16_t width, uint16_t height) {
  if (disp_) delete disp_;
  W_ = width; H_ = height;
  disp_ = new Adafruit_SSD1306(W_, H_, &wire, -1);
  GRAPH_SAMPLE_MS_ = (15UL * 60UL * 1000UL) / (unsigned long)W_;
  if (!disp_->begin(SSD1306_SWITCHCAPVCC, i2c_addr, true, false)) {
    present_ = false;
    return false;
  }
  present_ = true;
  disp_->clearDisplay();
  disp_->setTextSize(1);
  disp_->setTextColor(SSD1306_WHITE);
  disp_->setCursor(0,0);
  disp_->println(F("OLED ready"));
  disp_->display();
  last_update_ms_ = 0;
  last_sample_ms_ = 0;
  return true;
}

void OledUi::splash(const __FlashStringHelper* line1, const __FlashStringHelper* line2) {
  if (!present_) return;
  disp_->clearDisplay();
  disp_->setTextSize(1);
  disp_->setTextColor(SSD1306_WHITE);
  disp_->setCursor(0,0);
  if (line1) disp_->println(line1);
  if (line2) disp_->println(line2);
  disp_->display();
}

void OledUi::update(const Model& m, unsigned long min_period_ms) {
  if (!present_) return;

  unsigned long now = millis();

  // Periodic spark sampling at ~W_ samples across 15 minutes
  if (now - last_sample_ms_ >= GRAPH_SAMPLE_MS_) {
    last_sample_ms_ = now;
    pushSample_(m.co2_ppm,        m.co2_fresh,      M_CO2);
    pushSample_(m.sht45_rh,       m.sht45_rh_fresh, M_RH);
    pushSample_(m.tmp117_t,       m.tmp117_t_fresh, M_T);
    pushSample_(m.lps22df_p,      m.lps22df_p_fresh, M_P);
    pushSample_(m.tgs2611_v,      m.tgs2611_v_fresh, M_V2611);
    pushSample_(m.tgs2616_v,      m.tgs2616_v_fresh, M_V2616);
  }

  if (now - last_update_ms_ < min_period_ms) return;
  last_update_ms_ = now;

  disp_->clearDisplay();
  drawHeader_(m);

  // Choose one metric to show per update — you can customize page rotation in your app
  // Here we render CO2 by default; swap to others as needed or add a Page enum externally.
  drawGraph_(m.co2_ppm, m.co2_fresh, "CO2 ppm");

  disp_->display();
}

void OledUi::drawHeader_(const Model& m) {
  disp_->setTextSize(1);
  disp_->setTextColor(SSD1306_WHITE);
  disp_->setCursor(0,0);
  if (m.clock_text) disp_->println(m.clock_text);
  else disp_->println(F("Env Monitor"));
}

void OledUi::drawGraph_(float value, bool fresh, const char* label) {
  const int y0 = 16;
  const int gh = H_ - y0;
  // Title / value
  disp_->setCursor(0,8);
  disp_->setTextSize(1);
  disp_->print(label);
  disp_->print(F(": "));
  if (fresh && isfinite(value)) disp_->print(value);
  else disp_->print(F("NA"));

  // Frame
  disp_->drawRect(0, y0, W_, gh, SSD1306_WHITE);

  // Sparkline
  const Spark& s = sparks_[M_CO2]; // NOTE: using CO2 slot for this example
  if (!s.filled && s.head == 127 && s.ok[s.head] == 0) return; // nothing yet
  int prevx = -1, prevy = 0;
  uint16_t head = s.head;
  for (uint16_t i = 0; i < W_; ++i) {
    uint16_t idx = (head + i + 1) % W_;
    if (!s.ok[idx]) continue;
    float v = s.v[idx];
    // Simple autoscale between min/max seen in buffer
    float vmin =  1e9, vmax = -1e9;
    for (uint16_t j = 0; j < W_; ++j) if (s.ok[j]) { vmin = min(vmin, s.v[j]); vmax = max(vmax, s.v[j]); }
    if (vmax <= vmin) vmax = vmin + 1.0f;
    int x = i;
    int y = y0 + gh - 1 - int((v - vmin) * (gh - 1) / (vmax - vmin));
    if (prevx >= 0) disp_->drawLine(prevx, prevy, x, y, SSD1306_WHITE);
    prevx = x; prevy = y;
  }
}

void OledUi::pushSample_(float value, bool fresh, uint8_t which) {
  if (!isfinite(value)) fresh = false;
  sparks_[which].push(W_, isfinite(value) ? value : 0.0f, fresh);
}

} // namespace ui
