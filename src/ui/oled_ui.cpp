#include "ui/oled_ui.hpp"
#include <Adafruit_SSD1306.h>
#include <math.h>

namespace ui {

OledUi::OledUi() {}

bool OledUi::begin(TwoWire& wire, uint8_t i2c_addr, uint16_t width, uint16_t height) {
  if (disp_) delete disp_;
  W_ = width; H_ = height;
  disp_ = new Adafruit_SSD1306(W_, H_, &wire, -1);
  GRAPH_SAMPLE_MS_ = (15UL * 60UL * 1000UL) / (unsigned long)W_;
  for (auto& s : sparks_) s.configure(W_);

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
  last_update_ms_ = last_sample_ms_ = last_page_ms_ = 0;
  page_ = Page::CO2;
  autorotate_ = false;   // main.cpp controls pages unless you turn this on
  page_period_ms_ = 2000;
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

void OledUi::setPage(Page p) {
  page_ = p;
  last_page_ms_ = millis();
}

void OledUi::nextPage() {
  uint8_t x = static_cast<uint8_t>(page_);
  x = (x + 1) % static_cast<uint8_t>(Page::Count);
  page_ = static_cast<Page>(x);
  last_page_ms_ = millis();
}

void OledUi::setAutoRotate(bool on, unsigned long period_ms) {
  autorotate_ = on;
  page_period_ms_ = period_ms;
  last_page_ms_ = millis();
}

void OledUi::update(const ui::Model& m, unsigned long min_period_ms) {
  if (!present_) return;

  unsigned long now = millis();

  // Periodic spark sampling at ~W_ samples across 15 minutes
  if (now - last_sample_ms_ >= GRAPH_SAMPLE_MS_) {
    last_sample_ms_ = now;
    pushSample_(m.co2_ppm,   m.co2_fresh,      static_cast<uint8_t>(Signal::CO2));
    pushSample_(m.sht45_rh,  m.sht45_rh_fresh, static_cast<uint8_t>(Signal::RH));
    pushSample_(m.tmp117_t,  m.tmp117_t_fresh, static_cast<uint8_t>(Signal::T));
    pushSample_(m.lps22df_p, m.lps22df_p_fresh,static_cast<uint8_t>(Signal::P));
    pushSample_(m.tgs2611_v, m.tgs2611_v_fresh,static_cast<uint8_t>(Signal::V2611));
    pushSample_(m.tgs2616_v, m.tgs2616_v_fresh,static_cast<uint8_t>(Signal::V2616));
  }

  // Optional internal autorotate (OFF by default)
  if (autorotate_ && (now - last_page_ms_ >= page_period_ms_)) {
    nextPage();
  }

  if (now - last_update_ms_ < min_period_ms) return;
  last_update_ms_ = now;

  disp_->clearDisplay();
  drawHeader_(m);
  renderPage_(m);
  disp_->display();
}

void OledUi::drawHeader_(const ui::Model& m) {
  disp_->setTextSize(1);
  disp_->setTextColor(SSD1306_WHITE);
  disp_->setCursor(0,0);
  if (m.clock_text) disp_->println(m.clock_text);
  else              disp_->println(F("Env Monitor"));
}

void OledUi::drawGraph_(float value, bool fresh, const char* label) {
  const int y0 = 16;
  const int gh = H_ - y0;

  // Title/value
  disp_->setCursor(0,8);
  disp_->setTextSize(1);
  disp_->print(label);
  disp_->print(F(": "));
  if (fresh && isfinite(value)) disp_->print(value);
  else                          disp_->print(F("NA"));

  // Select spark by current page (more robust than string compare)
  const Spark* s_ptr = nullptr;
  switch (page_) {
    case Page::CO2:   s_ptr = &sparks_[static_cast<uint8_t>(Signal::CO2)]; break;
    case Page::RH:    s_ptr = &sparks_[static_cast<uint8_t>(Signal::RH)]; break;
    case Page::T:     s_ptr = &sparks_[static_cast<uint8_t>(Signal::T)]; break;
    case Page::P:     s_ptr = &sparks_[static_cast<uint8_t>(Signal::P)]; break;
    case Page::V2611: s_ptr = &sparks_[static_cast<uint8_t>(Signal::V2611)]; break;
    case Page::V2616: s_ptr = &sparks_[static_cast<uint8_t>(Signal::V2616)]; break;
    default: break;
  }
  if (!s_ptr) return;
  const Spark& s = *s_ptr;
  if (!s.count) return;

  // Autoscale: min/max over valid points
  float vmin =  1e9f, vmax = -1e9f;
  for (uint16_t j = 0; j < s.W; ++j) if (s.ok[j]) { vmin = min(vmin, s.v[j]); vmax = max(vmax, s.v[j]); }
  if (!(vmax > vmin)) { vmax = vmin + 1.0f; }

  // Draw spark
  int prevx = -1, prevy = 0;
  uint16_t head = s.head;
  for (uint16_t i = 0; i < s.W; ++i) {
    uint16_t idx = (head + i + 1) % s.W;
    if (!s.ok[idx]) continue;
    float v = s.v[idx];
    int x = i;
    int y = y0 + gh - 1 - int((v - vmin) * (gh - 1) / (vmax - vmin));
    if (prevx >= 0) disp_->drawLine(prevx, prevy, x, y, SSD1306_WHITE);
    prevx = x; prevy = y;
  }
}

void OledUi::pushSample_(float value, bool fresh, uint8_t which) {
  if (!isfinite(value)) fresh = false;
  if (which >= static_cast<uint8_t>(Signal::COUNT)) return;
  sparks_[which].push(isfinite(value) ? value : 0.0f, fresh);
}

void OledUi::renderPage_(const ui::Model& m) {
  switch (page_) {
    case Page::CO2: {
      char lab[24];
      if (m.co2_n > 1) snprintf(lab, sizeof(lab), "CO2 #%u (ppm)", unsigned(m.co2_idx + 1));
      else             snprintf(lab, sizeof(lab), "CO2 (ppm)");
      drawGraph_(m.co2_ppm, m.co2_fresh, lab);
      break;
    }
    case Page::RH: {
      char lab[24];
      if (m.rh_n > 1) snprintf(lab, sizeof(lab), "RH #%u (%%)", unsigned(m.rh_idx + 1));
      else            snprintf(lab, sizeof(lab), "RH (%%)");
      drawGraph_(m.sht45_rh, m.sht45_rh_fresh, lab);
      break;
    }
    case Page::T: {
      char lab[24];
      if (m.t_n > 1) snprintf(lab, sizeof(lab), "Temp #%u (C)", unsigned(m.t_idx + 1));
      else           snprintf(lab, sizeof(lab), "Temp (C)");
      drawGraph_(m.tmp117_t, m.tmp117_t_fresh, lab);
      break;
    }
    case Page::P: {
      char lab[24];
      if (m.p_n > 1) snprintf(lab, sizeof(lab), "Press #%u (hPa)", unsigned(m.p_idx + 1));
      else           snprintf(lab, sizeof(lab), "Press (hPa)");
      drawGraph_(m.lps22df_p, m.lps22df_p_fresh, lab);
      break;
    }
    case Page::V2611: {
      char lab[24];
      if (m.v2611_n > 1) snprintf(lab, sizeof(lab), "TGS2611 #%u (V)", unsigned(m.v2611_idx + 1));
      else               snprintf(lab, sizeof(lab), "TGS2611 (V)");
      drawGraph_(m.tgs2611_v, m.tgs2611_v_fresh, lab);
      break;
    }
    case Page::V2616: {
      char lab[24];
      if (m.v2616_n > 1) snprintf(lab, sizeof(lab), "TGS2616 #%u (V)", unsigned(m.v2616_idx + 1));
      else               snprintf(lab, sizeof(lab), "TGS2616 (V)");
      drawGraph_(m.tgs2616_v, m.tgs2616_v_fresh, lab);
      break;
    }
    default: break;
  }
}

} // namespace ui
