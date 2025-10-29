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
    last_page_ms_ = 0;
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

    // Auto-rotate pages
    if (autorotate_ && (now - last_page_ms_ >= page_period_ms_)) {
      last_page_ms_ = now;
      nextPage();
    }

    if (now - last_update_ms_ < min_period_ms) return;
    last_update_ms_ = now;

    disp_->clearDisplay();
    drawHeader_(m);
    renderPage_(m);
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
    // disp_->drawRect(0, y0, W_, gh, SSD1306_WHITE);

    // Sparkline
    // Select the sparkline matching the label/value
    const Spark* s_ptr = nullptr;
    if      (strcmp(label, "CO2 ppm") == 0)      s_ptr = &sparks_[M_CO2];
    else if (strcmp(label, "RH %") == 0)         s_ptr = &sparks_[M_RH];
    else if (strcmp(label, "Temp C") == 0)       s_ptr = &sparks_[M_T];
    else if (strcmp(label, "Press hPa") == 0)    s_ptr = &sparks_[M_P];
    else if (strcmp(label, "TGS2611 V") == 0)    s_ptr = &sparks_[M_V2611];
    else if (strcmp(label, "TGS2616 V") == 0)    s_ptr = &sparks_[M_V2616];
    if (!s_ptr) return;
    const Spark& s = *s_ptr;

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

  void OledUi::renderPage_(const Model& m) {
    switch (page_) {
      case Page::Time: {
        // Big text time view
        disp_->setTextSize(1);
        disp_->setCursor(0,8);
        disp_->print(F("Date & Time"));
        disp_->setTextSize(2);
        disp_->setCursor(0, 24);
        if (m.clock_text) disp_->print(m.clock_text);
        else              disp_->print(F("NA"));
        break;
      }
      case Page::CO2: {
        char lab[20];
        if (m.co2_n > 1) snprintf(lab, sizeof(lab), "CO2 #%u (ppm)", unsigned(m.co2_idx + 1));
        else             snprintf(lab, sizeof(lab), "CO2 (ppm)");
        drawGraph_(m.co2_ppm, m.co2_fresh, lab);
        break;
      }
      case Page::RH: {
        char lab[16];
        if (m.rh_n > 1) snprintf(lab, sizeof(lab), "RH #%u (%%)", unsigned(m.rh_idx + 1));
        else            snprintf(lab, sizeof(lab), "RH (%%)");
        drawGraph_(m.sht45_rh, m.sht45_rh_fresh, lab);
        break;
      }
      case Page::T: {
        char lab[20];
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

  void OledUi::setPage(Page p) {
    page_ = p;
    last_page_ms_ = millis();
  }

  void OledUi::nextPage() {
    uint8_t x = static_cast<uint8_t>(page_);
    x = (x + 1) % static_cast<uint8_t>(Page::Count);
    page_ = static_cast<Page>(x);
  }

  void OledUi::setAutoRotate(bool on, unsigned long period_ms) {
    autorotate_ = on;
    page_period_ms_ = period_ms;
    last_page_ms_ = millis();
  }

}