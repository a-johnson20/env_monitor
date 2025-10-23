// Library includes
#include <Arduino.h>
#include <esp_idf_version.h>   // for ESP_IDF_VERSION / ESP_IDF_VERSION_VAL
#include <Wire.h>
#include <SensirionI2cScd4x.h>
#include <RV-3028-C7.h>          // constiko / RV-3028_C7-Arduino_Library
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h> // OLED
#include <driver/ledc.h>   // ESP-IDF LEDC driver
#include <math.h> // For sparklines
#include <array>

#include "app/boot_read.hpp"
#include "hal/mux_map.hpp"
#include "hal/tca9548a.hpp"
#include "hal/board.hpp"
#include "hal/i2c_addresses.hpp"
#include "logging/sd_logger.hpp"
#include "app/log_format.hpp"
#include "app/calibration.hpp"
#include "common/tgs_lookup_tables.hpp"
#include "../common/calib/tgs_calibration.hpp"

// Namespaces using
using hal::Mux::Ch;

static hal::Mux::Tca9548State muxStateWire;

// ---------- I2C buses ----------
TwoWire WireRTC = TwoWire(1);    // RTC + OLED on I2C1 (GPIO 15/16); sensors stay on default Wire

// ---------- Pins / I2C / MUX ----------
#define TCA9548A_ADDR   0x70

// Sensor bus (through TCA9548A)
#define SDA 5
#define SCL 6

// RTC + OLED on a separate I2C bus (no mux)
#define RTC_SDA 15
#define RTC_SCL 16

#define LDO_Sensors_EN  41

// ---------- Pump PWM (GPIO21) ----------
#define PUMP_PWM_PIN     21
#define PUMP_PWM_FREQ    1000              // Hz
#define PUMP_PWM_RES     LEDC_TIMER_12_BIT // 12-bit
#define PUMP_LEDC_MODE   LEDC_LOW_SPEED_MODE
#define PUMP_LEDC_TIMER  LEDC_TIMER_0
#define PUMP_LEDC_CH     LEDC_CHANNEL_0

// ---------- OLED ----------
#define OLED_WIDTH    128
#define OLED_HEIGHT   64
#define OLED_ADDR     0x3D
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &WireRTC, -1);
bool oled_present = false;

// ---------- Error Definition ----------
#ifdef NO_ERROR
#undef NO_ERROR
#endif
#define NO_ERROR 0

// ---------- Driver objects ----------
SensirionI2cScd4x scd4x;

// RTC
RV3028 rtc;
bool rtc_present = false;

// ---------- Small data structs ----------
struct Scd4xReading {
  bool present = false, started = false, ready = false;
  unsigned long started_at_ms = 0;
  unsigned long last_ok_ms = 0; // timestamp of last good read - to check freshness
  uint16_t co2 = 0;
  float temp = NAN, rh = NAN;
};

struct Sht45Reading { bool valid = false; float temp = NAN, rh = NAN; };
struct Lps22dfReading {
  bool p_ready = false, t_ready = false, present = false;
  bool configured = false;
  float pressure = NAN, temp = NAN;
  unsigned long last_ok_ms = 0;
};
struct Tmp117Reading { bool valid = false; float temp = NAN; };
struct Ads1113Reading { bool valid = false; int16_t raw = 0; float volts = NAN; };

struct Readings {
  Scd4xReading scd4x;
  Sht45Reading sht45;
  Lps22dfReading lps22df;
  Tmp117Reading tmp117;
  Ads1113Reading ads;
} readings;

struct RunningAvg {
  double sum = 0.0; uint32_t count = 0;
  inline void add(float v) { if (!isnan(v)) { sum += v; count++; } }
  inline float mean() const { return count ? (float)(sum / count) : NAN; }
  inline void reset() { sum = 0.0; count = 0; }
};

// Per-probe running windows (counts match config.hpp channel lists)
constexpr size_t N_SCD4X =    hal::Mux::SCD4x.size();
constexpr size_t N_TRHP  =    hal::Mux::TRHP.size();
constexpr size_t N_TGS2611 =  hal::Mux::TGS2611.size();
constexpr size_t N_TGS2616 =  hal::Mux::TGS2616.size();

// Per-SCD4x instance readings (one struct per node)
std::array<Scd4xReading, N_SCD4X> scd4x_nodes{};

// Per-TRHP station averages (one set per mux channel)
std::array<Lps22dfReading, N_TRHP> lps22df_nodes{};

std::array<RunningAvg, N_TRHP> win_trhp_sht45_t{};
std::array<RunningAvg, N_TRHP> win_trhp_sht45_rh{};
std::array<RunningAvg, N_TRHP> win_trhp_tmp117_t{};
std::array<RunningAvg, N_TRHP> win_trhp_lps_p{};
std::array<RunningAvg, N_TRHP> win_trhp_lps_t{};

std::array<RunningAvg, N_TGS2611> win_tgs2611_raw{};
std::array<RunningAvg, N_TGS2611> win_tgs2611_v{};
std::array<RunningAvg, N_TGS2616> win_tgs2616_raw{};
std::array<RunningAvg, N_TGS2616> win_tgs2616_v{};

// --- SCD4x sync for aligned commits ---
static const unsigned long SCD4X_SYNC_TIMEOUT_MS = 10000; // 10s fallback
constexpr unsigned long SCD_FRESH_MS = 10000UL;  // 10s limit for freshness
std::array<bool, N_SCD4X> scd4x_fresh{};  // new sample since last commit?
unsigned long scd4x_tick_start_ms = 0;    // start time of the current averaging window
static unsigned long no_scd_last_commit_ms = 0; // periodic backup commit for the no-SCD4x case

// ---- 15-minute sparkline history ----
static const uint16_t SPARK_W   = OLED_WIDTH;              // 128
static const uint16_t SPARK_H   = OLED_HEIGHT - 16;        // graph area height (below header)
static const uint16_t SPARK_Y0  = 16;                       // graph top
static const unsigned long GRAPH_SPAN_MS   = 15UL * 60UL * 1000UL; // 15 min
static const unsigned long GRAPH_SAMPLE_MS = GRAPH_SPAN_MS / SPARK_W; // ~7031 ms

struct Spark {
  float   v[SPARK_W];
  uint8_t valid[SPARK_W];
  uint16_t head = SPARK_W - 1; // last written index
  bool filled = false;

  inline void push(float x, bool ok) {
    head = (head + 1) % SPARK_W;
    v[head] = x;
    valid[head] = ok ? 1 : 0;
    if (head == SPARK_W - 1) filled = true;
  }

  inline bool hasData() const { 
    if (filled) return true;
    for (uint16_t i=0;i<=head;i++) if (valid[i]) return true;
    return false;
  }

  void draw(Adafruit_SSD1306& d, int y0, int h) const {
    if (!hasData()) return;
    float mn = INFINITY, mx = -INFINITY;
    for (uint16_t i=0;i<SPARK_W;i++) if (valid[i]) { if (v[i]<mn) mn=v[i]; if (v[i]>mx) mx=v[i]; }
    if (!(mx>mn)) {               // all same or single point
      if (isfinite(mn)) {
        int y = y0 + h/2;
        d.drawLine(0, y, SPARK_W-1, y, SSD1306_WHITE);
      }
      return;
    }
    // small padding so the trace doesn't touch edges
    float pad = 0.05f * (mx - mn);
    mn -= pad; mx += pad;

    int prevx = -1, prevy = -1;
    for (uint16_t i=0;i<SPARK_W;i++) {
      uint16_t idx = (head + 1 + i) % SPARK_W;   // oldest → newest maps to x=0..127
      if (!valid[idx]) { prevx = -1; continue; }
      float val = v[idx];
      int x = i;
      int y = y0 + h - 1 - (int)((val - mn) * (h - 1) / (mx - mn));
      if (prevx >= 0) d.drawLine(prevx, prevy, x, y, SSD1306_WHITE);
      prevx = x; prevy = y;
    }
  }
};

// One sparkline per *sensor* for the displayed metrics
std::array<Spark, N_SCD4X>   hist_scd4x_co2{};
std::array<Spark, N_TRHP>    hist_trhp_rh{}, hist_tmp117_t{}, hist_lps22df_p{};
std::array<Spark, N_TGS2611> hist_tgs2611_v{};
std::array<Spark, N_TGS2616> hist_tgs2616_v{};

unsigned long last_graph_sample_ms = 0;

// ---------- Utils ----------
uint8_t crc8(const uint8_t* data, int len) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < len; i++) {
    crc ^= data[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? ((uint8_t)((crc << 1) ^ 0x31)) : (uint8_t)(crc << 1);
  }
  return crc;
}
static void print_avg_kv(const char* key, const RunningAvg& a, int digits, const char* unit, bool trailingComma = true) {
  Serial.print(key);
  if (a.count) Serial.print(a.mean(), digits); else Serial.print("NA");
  if (unit) Serial.print(unit);
  if (trailingComma) Serial.print(", ");
}

// ---------- Pump helpers ----------

float pump_percent = 0.0f;      // current setting (0..100)

// Init PWM on GPIO21
static void pump_begin() {
  ledc_timer_config_t tcfg = {};
  tcfg.speed_mode       = PUMP_LEDC_MODE;
  tcfg.timer_num        = PUMP_LEDC_TIMER;
  tcfg.duty_resolution  = PUMP_PWM_RES;
  tcfg.freq_hz          = PUMP_PWM_FREQ;
  tcfg.clk_cfg          = LEDC_AUTO_CLK;
  ledc_timer_config(&tcfg);

  ledc_channel_config_t ccfg = {};
  ccfg.gpio_num       = PUMP_PWM_PIN;
  ccfg.speed_mode     = PUMP_LEDC_MODE;
  ccfg.channel        = PUMP_LEDC_CH;
  ccfg.intr_type      = LEDC_INTR_DISABLE;
  ccfg.timer_sel      = PUMP_LEDC_TIMER;
  ccfg.duty           = 0;        // start off
  ccfg.hpoint         = 0;
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 0, 0)
  ccfg.flags.output_invert = 0;   // set to 1 if your buffer inverts
#endif
  ledc_channel_config(&ccfg);
}

// Set duty as percent (0..100)
static void pump_set_percent(float pct) {
  if (pct < 0) pct = 0; if (pct > 100) pct = 100;
  const uint32_t maxDuty = (1u << LEDC_TIMER_12_BIT) - 1;
  uint32_t duty = (uint32_t)((pct / 100.0f) * maxDuty + 0.5f);
  ledc_set_duty(PUMP_LEDC_MODE, PUMP_LEDC_CH, duty);
  ledc_update_duty(PUMP_LEDC_MODE, PUMP_LEDC_CH);
}

static inline void pump_off() { pump_set_percent(0); }
static inline void pump_on()  { pump_set_percent(100); }

// ---------- RTC helpers ----------
static void format_timestamp(char* out, size_t n) {
  if (rtc_present && rtc.updateTime()) {
    int y = rtc.getYear(); if (y < 100) y += 2000;
    snprintf(out, n, "%04d-%02d-%02d %02d:%02d:%02d",
             y, rtc.getMonth(), rtc.getDate(), rtc.getHours(), rtc.getMinutes(), rtc.getSeconds());
  } else {
    snprintf(out, n, "RTC NA (%lus)", millis()/1000);
  }
}
static void print_timestamp() { char buf[24]; format_timestamp(buf, sizeof(buf)); Serial.print("["); Serial.print(buf); Serial.print("] "); }

static void format_timestamp_no_sec(char* out, size_t n) {
  if (rtc_present && rtc.updateTime()) {
    int y = rtc.getYear(); if (y < 100) y += 2000;
    snprintf(out, n, "%04d-%02d-%02d %02d:%02d",
             y, rtc.getMonth(), rtc.getDate(),
             rtc.getHours(), rtc.getMinutes());
  } else {
    snprintf(out, n, "RTC NA");
  }
}

static void sample_graphs_if_due() {
  unsigned long now = millis();
  if (now - last_graph_sample_ms < GRAPH_SAMPLE_MS) return;
  last_graph_sample_ms = now;

  // SCD4x (per node)
  for (size_t i=0;i<N_SCD4X;i++) {
    const auto& n = scd4x_nodes[i];
    bool ok = n.last_ok_ms && (millis() - n.last_ok_ms <= SCD_FRESH_MS);
    float v = ok ? (float)n.co2 : NAN;
    hist_scd4x_co2[i].push(v, ok);
  }

  // TRHP per station
  for (size_t i=0;i<N_TRHP;i++) {
    hist_trhp_rh[i].push(     win_trhp_sht45_rh[i].mean(), win_trhp_sht45_rh[i].count>0);
    hist_tmp117_t[i].push(    win_trhp_tmp117_t[i].mean(), win_trhp_tmp117_t[i].count>0);
    hist_lps22df_p[i].push(   win_trhp_lps_p[i].mean(),    win_trhp_lps_p[i].count>0);
  }

  // TGS per probe
  for (size_t i=0;i<N_TGS2611;i++) {
    hist_tgs2611_v[i].push(win_tgs2611_v[i].mean(), win_tgs2611_v[i].count>0);
  }
  for (size_t i=0;i<N_TGS2616;i++) {
    hist_tgs2616_v[i].push(win_tgs2616_v[i].mean(), win_tgs2616_v[i].count>0);
  }

}

static void reset_windows_and_flags() {
  for (auto &w : win_trhp_sht45_t) w.reset();
  for (auto &w : win_trhp_sht45_rh) w.reset();
  for (auto &w : win_trhp_tmp117_t) w.reset();
  for (auto &w : win_trhp_lps_p)    w.reset();
  for (auto &w : win_trhp_lps_t)    w.reset();

  for (auto &w : win_tgs2611_raw) w.reset();
  for (auto &w : win_tgs2611_v)   w.reset();
  for (auto &w : win_tgs2616_raw) w.reset();
  for (auto &w : win_tgs2616_v)   w.reset();

  for (size_t k = 0; k < N_SCD4X; ++k) scd4x_fresh[k] = false;
}


static void commit_and_reset_all_windows() {
  // Timestamp (always)
  char ts[24];
  format_timestamp(ts, sizeof(ts));

  // Build the line (always)
  String line;
  line.reserve(512);
  line += ts;

  // SCD4x
  for (size_t i = 0; i < N_SCD4X; ++i) {
    const auto& n = scd4x_nodes[i];
    bool fresh = n.last_ok_ms && (millis() - n.last_ok_ms <= SCD_FRESH_MS);
    line += ','; line += (fresh ? String((unsigned long)n.co2) : "NA");
    line += ','; line += (fresh ? String(n.temp, 2)             : "NA");
    line += ','; line += (fresh ? String(n.rh,   2)             : "NA");
  }

  // TRHP avgs
  for (size_t i = 0; i < N_TRHP; ++i) {
    line += ','; line += (win_trhp_sht45_t[i].count ? String(win_trhp_sht45_t[i].mean(), 2) : "NA");
    line += ','; line += (win_trhp_sht45_rh[i].count ? String(win_trhp_sht45_rh[i].mean(), 2) : "NA");
    line += ','; line += (win_trhp_tmp117_t[i].count ? String(win_trhp_tmp117_t[i].mean(), 2) : "NA");
    line += ','; line += (win_trhp_lps_p[i].count    ? String(win_trhp_lps_p[i].mean(),  2) : "NA");
    line += ','; line += (win_trhp_lps_t[i].count    ? String(win_trhp_lps_t[i].mean(),  2) : "NA");
  }

  // TGS avgs
  for (size_t i = 0; i < N_TGS2611; ++i) {
    line += ','; line += (win_tgs2611_raw[i].count ? String(win_tgs2611_raw[i].mean(), 0) : "NA");
    line += ','; line += (win_tgs2611_v[i].count   ? String(win_tgs2611_v[i].mean(),   5) : "NA");
  }
  for (size_t i = 0; i < N_TGS2616; ++i) {
    line += ','; line += (win_tgs2616_raw[i].count ? String(win_tgs2616_raw[i].mean(), 0) : "NA");
    line += ','; line += (win_tgs2616_v[i].count   ? String(win_tgs2616_v[i].mean(),   5) : "NA");
  }

  // Always print to Serial
  Serial.println(line);

  // Only SD work is gated
  if (sd_logger::is_mounted()) {
    const String path = logfmt::current_log_path(rtc_present, rtc);
    sd_logger::ensure_header(path, logfmt::make_header(N_SCD4X, N_TRHP, N_TGS2611, N_TGS2616));
    sd_logger::append_line(path, line);
  }

  // Reset per-window state (your new helper)
  reset_windows_and_flags();
}


// ---------- SCD4x ----------
bool scd4x_present() { Wire.beginTransmission(SCD41_I2C_ADDR_62); return Wire.endTransmission() == 0; }
void scd4x_ensure_running(Scd4xReading &st) {
  if (st.started) return;
  st.present = scd4x_present(); if (!st.present) return;
  scd4x.wakeUp(); scd4x.reinit();
  if (scd4x.startPeriodicMeasurement() == NO_ERROR) { st.started = true; st.started_at_ms = millis(); }
}
bool scd4x_read_if_ready(Scd4xReading &out) {
  if (!out.started) return false;
  if (millis() - out.started_at_ms < 7000) return false;
  bool ready = false;
  if (scd4x.getDataReadyStatus(ready) != NO_ERROR) { out.started = false; return false; }
  if (!ready) return false;
  uint16_t co2; float t, rh;
  if (scd4x.readMeasurement(co2, t, rh) != NO_ERROR) { out.started = false; return false; }
  out.co2 = co2; out.temp = t; out.rh = rh; out.ready = true; out.last_ok_ms = millis(); return true;
}

// ---------- SHT45 ----------
bool sht45_measure(Sht45Reading &out) {
  const uint8_t a = 0x44;
  Wire.beginTransmission(a); Wire.write(0xFD); Wire.endTransmission();
  delay(10);
  Wire.requestFrom(a, (uint8_t)6);
  if (Wire.available() != 6) return false;
  uint8_t b[6]; for (int i=0;i<6;i++) b[i]=Wire.read();
  if (crc8(b,2) != b[2] || crc8(&b[3],2) != b[5]) return false;
  uint16_t rawT = (b[0] << 8) | b[1];
  uint16_t rawRH = (b[3] << 8) | b[4];
  out.temp = -45.0f + 175.0f * (rawT / 65535.0f);
  out.rh   =  -6.0f + 125.0f * (rawRH / 65535.0f);
  if (out.rh < 0) out.rh = 0; if (out.rh > 100) out.rh = 100;
  out.valid = true; return true;
}

// ---------- TMP117 ----------
bool tmp117_measure(Tmp117Reading &out) {
  const uint8_t a = 0x48;
  Wire.beginTransmission(a); Wire.write(0x00); Wire.endTransmission(false);
  Wire.requestFrom(a, (uint8_t)2);
  if (Wire.available() != 2) return false;
  int16_t raw = (Wire.read() << 8) | Wire.read();
  out.temp = raw * 0.0078125f; out.valid = true; return true;
}

// ---------- LPS22DF ----------
static const unsigned long LPS22DF_RECFG_MS = 1500;
static void lps22df_write(uint8_t reg, uint8_t val) { const uint8_t a = 0x5D; Wire.beginTransmission(a); Wire.write(reg); Wire.write(val); Wire.endTransmission(); }
static const uint8_t LPS22DF_ADDR = 0x5D;
static bool lps22df_read_u8(uint8_t reg, uint8_t& v) {
  Wire.beginTransmission(LPS22DF_ADDR); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(LPS22DF_ADDR, (uint8_t)1) != 1) return false;
  v = Wire.read(); return true;
}
bool lps22df_begin_on_selected(Lps22dfReading &state) {
  uint8_t who = 0; if (!lps22df_read_u8(0x0F, who) || who != 0xB4) { state.present=false; state.configured=false; return false; }
  lps22df_write(0x11, 0x04); for (uint8_t i=0;i<10;i++){ uint8_t c2; if (lps22df_read_u8(0x11,c2) && ((c2&0x04)==0)) break; delay(2); }
  lps22df_write(0x11, 0x80); for (uint8_t i=0;i<10;i++){ uint8_t c2; if (lps22df_read_u8(0x11,c2) && ((c2&0x80)==0)) break; delay(2); }
  lps22df_write(0x10, 0x33); lps22df_write(0x11, 0x08); lps22df_write(0x12, 0x01);
  state.present=true; state.configured=true; state.last_ok_ms=millis(); return true;
}
bool lps22df_read(Lps22dfReading &out) {
  const uint8_t a = 0x5D;
  Wire.beginTransmission(a); Wire.write(0x27); Wire.endTransmission(false);
  Wire.requestFrom(a, (uint8_t)1); if (!Wire.available()) return false;
  uint8_t status = Wire.read(); bool got=false;
  if (status & 0x01) {
    Wire.beginTransmission(a); Wire.write(0x28); Wire.endTransmission(false);
    Wire.requestFrom(a, (uint8_t)3);
    if (Wire.available()==3) { uint8_t xl=Wire.read(), l=Wire.read(), h=Wire.read();
      int32_t raw = (int32_t)((h<<16)|(l<<8)|xl); if (raw & 0x800000) raw |= 0xFF000000;
      out.pressure = raw / 4096.0f; out.p_ready=true; got=true; }
  }
  if (status & 0x02) {
    Wire.beginTransmission(a); Wire.write(0x2B); Wire.endTransmission(false);
    Wire.requestFrom(a, (uint8_t)2);
    if (Wire.available()==2) { uint8_t tl=Wire.read(), th=Wire.read(); int16_t raw=(int16_t)((th<<8)|tl);
      out.temp = raw / 100.0f; out.t_ready=true; got=true; }
  }
  if (got) out.last_ok_ms = millis();
  return got;
}
bool lps22df_read_with_autorecover(Lps22dfReading &st) {
  if (!st.configured || (millis() - st.last_ok_ms > LPS22DF_RECFG_MS)) if (!lps22df_begin_on_selected(st)) return false;
  st.p_ready = st.t_ready = false; return lps22df_read(st);
}

// ---------- ADS1113 ----------
static const uint8_t ADS1113_ADDR = 0x48;
static const float    ADS1113_LSB_V = 0.000125f;
bool ads1113_single_shot(int16_t &raw) {
  const uint8_t a = ADS1113_ADDR;
  uint16_t cfg = (1u<<15)|(0b100<<12)|(0b001<<9)|(1u<<8)|(0b100<<5)|(0b11);
  Wire.beginTransmission(a); Wire.write(0x01); Wire.write((uint8_t)(cfg>>8)); Wire.write((uint8_t)(cfg&0xFF));
  if (Wire.endTransmission()!=0) return false;
  delay(9);
  Wire.beginTransmission(a); Wire.write(0x00); Wire.endTransmission(false);
  Wire.requestFrom(a, (uint8_t)2); if (Wire.available()!=2) return false;
  raw = (int16_t)((Wire.read()<<8)|Wire.read()); return true;
}

// ---------- OLED UI ----------
enum ScreenPage : uint8_t { SCR_TIME=0, SCR_CO2, SCR_SHT45_RH, SCR_TMP117_T, SCR_LPS22DF_P, SCR_TGS2611_V, SCR_TGS2616_V, SCR_COUNT };
static ScreenPage screen_index = SCR_TIME;
unsigned long last_screen_ms = 0;
const unsigned long SCREEN_PERIOD_MS = 2000;

static uint8_t sub_idx = 0;

static uint8_t subcount_for(uint8_t page) {
  switch (page) {
    case SCR_TGS2611_V: return N_TGS2611 ? (uint8_t)N_TGS2611 : 1;
    case SCR_TGS2616_V: return N_TGS2616 ? (uint8_t)N_TGS2616 : 1;
    case SCR_CO2:       return N_SCD4X  ? (uint8_t)N_SCD4X  : 1;
    case SCR_SHT45_RH:  return N_TRHP   ? (uint8_t)N_TRHP   : 1;
    case SCR_TMP117_T:  return N_TRHP   ? (uint8_t)N_TRHP   : 1;
    case SCR_LPS22DF_P: return N_TRHP   ? (uint8_t)N_TRHP   : 1;
    default:            return 1;
  }
}

/* NOT USED ANYMORE
uint8_t screen_index = SCR_TIME;

uint8_t scd4x_oled_idx   = 0;
uint8_t trhp_oled_idx    = 0;     // used for SHT45 RH, TMP117 T, LPS22DF P
uint8_t tgs2611_oled_idx = 0;
uint8_t tgs2616_oled_idx = 0;
*/

static void oled_show_value_and_graph(const char* title, const char* value, const Spark& s) {
  if (!oled_present) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);

  // Header + value
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print(title);
  display.setCursor(0, 8);
  display.print(value);

  // Graph (no axes/labels)
  s.draw(display, SPARK_Y0, SPARK_H);

  display.display();
}

// e.g. for time
static void oled_show_text(const char* title, const char* value) {
  if (!oled_present) return;
  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1); display.setCursor(0, 0); display.print(title);
  display.setTextSize(2); display.setCursor(0, 24); display.print(value);
  display.display();
}

static void update_oled_if_due() {
  if (!oled_present) return;
  unsigned long now = millis(); if (now - last_screen_ms < SCREEN_PERIOD_MS) return;
  last_screen_ms = now;

  char line[48];
  switch (screen_index) {
    case SCR_TIME: {
    format_timestamp_no_sec(line, sizeof(line));
    oled_show_text("Date & Time", line);
    break;
    }

    case SCR_CO2: {
      if (N_SCD4X == 0) break;
      const auto& n = scd4x_nodes[sub_idx];
      char line[24];
      bool fresh = n.last_ok_ms && (millis() - n.last_ok_ms <= SCD_FRESH_MS);
      if (fresh) snprintf(line, sizeof(line), "%u ppm", (unsigned)n.co2);
      else       snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "SCD41 CO2 #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_scd4x_co2[sub_idx]);
      break;
    }

    case SCR_SHT45_RH: {
      if (N_TRHP == 0) break;
      char line[24];
      if (win_trhp_sht45_rh[sub_idx].count) snprintf(line, sizeof(line), "%.2f %%", win_trhp_sht45_rh[sub_idx].mean());
      else snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "SHT45 RH #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_trhp_rh[sub_idx]);
      break;
    }

    case SCR_TMP117_T: {
      if (N_TRHP == 0) break;
      char line[24];
      if (win_trhp_tmp117_t[sub_idx].count) snprintf(line, sizeof(line), "%.2f C", win_trhp_tmp117_t[sub_idx].mean());
      else snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "TMP117 T #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_tmp117_t[sub_idx]);
      break;
    }

    case SCR_LPS22DF_P: {
      if (N_TRHP == 0) break;
      char line[24];
      if (win_trhp_lps_p[sub_idx].count) snprintf(line, sizeof(line), "%.2f hPa", win_trhp_lps_p[sub_idx].mean());
      else snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "LPS22DF P #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_lps22df_p[sub_idx]);
      break;
    }

    case SCR_TGS2611_V: {
      if (N_TGS2611 == 0) break;
      char line[24];
      if (win_tgs2611_v[sub_idx].count) snprintf(line, sizeof(line), "%.5f V", win_tgs2611_v[sub_idx].mean());
      else snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "TGS2611 CH4 #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_tgs2611_v[sub_idx]);
      break;
    }

    case SCR_TGS2616_V: {
      if (N_TGS2616 == 0) break;
      char line[24];
      if (win_tgs2616_v[sub_idx].count) snprintf(line, sizeof(line), "%.5f V", win_tgs2616_v[sub_idx].mean());
      else snprintf(line, sizeof(line), "NA");
      char title[24]; snprintf(title, sizeof(title), "TGS2616 H2 #%u", unsigned(sub_idx+1));
      oled_show_value_and_graph(title, line, hist_tgs2616_v[sub_idx]);
      break;
    }

  }
  // advance sub-page; if finished this group, go to next main page
  sub_idx++;
  if (sub_idx >= subcount_for(screen_index)) {
    sub_idx = 0;
    screen_index = static_cast<ScreenPage>(
                 (static_cast<uint8_t>(screen_index) + 1) % SCR_COUNT
               );
  }
}

namespace {
  static inline uint8_t to_u8(hal::Mux::Ch ch) { return static_cast<uint8_t>(ch); }
}

void setup() {
  Serial.begin(115200);
  Serial.println("Serial open");

  pinMode(LDO_Sensors_EN, OUTPUT);
  digitalWrite(LDO_Sensors_EN, HIGH); // power sensors

  // Sensors I2C (through TCA9548A)
  Wire.begin(SDA, SCL);
  Wire.setClock(100000);
  delay(50);

  // RTC + OLED I2C (direct, no mux) on GPIO 15/16
  WireRTC.begin(RTC_SDA, RTC_SCL);
  WireRTC.setClock(400000);

  // OLED on WireRTC
  oled_present = display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDR, false, false); // periphBegin=false (we already began WireRTC)
  if (oled_present) {
    display.clearDisplay(); display.setTextSize(1); display.setTextColor(SSD1306_WHITE);
    display.setCursor(0,0); display.println("OLED ready"); display.display();
  } else {
    Serial.println("WARNING: SSD1306 OLED not found on RTC bus.");
  }

  // RTC on WireRTC
  rtc_present = rtc.begin(WireRTC);
  if (rtc_present) {
    Serial.println("RV-3028 RTC ready");
  } else {
    Serial.println("WARNING: RV-3028 not found on RTC bus.");
  }

  // Mux present?
  Wire.beginTransmission(TCA9548A_ADDR);
  if (Wire.endTransmission() != 0) {
    Serial.println("ERROR: TCA9548A not found at 0x70!");
    while (1) delay(1000);
  }
  Serial.println("TCA9548A connected");

  // Calibrate TGS2611 channels
  app::calibrate_all_tgs2611(muxStateWire);

  // Calibrate TGS2616 channels
  app::calibrate_all_tgs2616(muxStateWire);

  // Bind SCD4x driver once
  scd4x.begin(Wire, SCD41_I2C_ADDR_62);

  // Print SCD4x serial numbers
  for (auto ch : hal::Mux::SCD4x) if (select_channel(Wire, ch, muxStateWire)) {
    uint64_t sn = 0;
    if (scd4x.getSerialNumber(sn) == NO_ERROR) {
      Serial.printf("SCD4x[ch=%u] serial: %08X%08X\n",
                    to_u8(ch), (uint32_t)(sn >> 32), (uint32_t)(sn & 0xFFFFFFFF));
    }
  }

  // Init LPS22DF on every configured TRHP channel
  size_t i = 0;
  for (auto ch : hal::Mux::TRHP) {
    if (select_channel(Wire, ch, muxStateWire)) {
      if (!lps22df_begin_on_selected(lps22df_nodes[i])) {
        Serial.printf("LPS22DF init failed on ch %u\n", to_u8(ch));
      }
    }
    ++i;
  }

  if (!sd_logger::begin()) {
    Serial.println("WARNING: SD logging disabled.");
  }

  pump_begin();
  pump_set_percent(50.0f);  // start at 50% (pick what you want)

  delay(10000); // Give time to open serial monitor

  // Print TGS state on boot (ID + CRC + wiper + kΩ).
  // Set 'true' to include the extra +1 kΩ series resistor in the reported value.
  app::print_tgs_boot_status(
      muxStateWire,      // your local mux state
      Wire,                        // I²C bus you use for sensors
      hal::I2CAddr::TCA9548A,      // mux address from your central header
      false  // set true if you want +1 kΩ added in the printout
  );
}

void loop() {

  // --------- SCD4x ---------
  {
    size_t i = 0;
    for (auto ch : hal::Mux::SCD4x) {
      if (!select_channel(Wire, ch, muxStateWire)) { ++i; continue; }

      // Ensure running (sets .present if detected)
      scd4x_ensure_running(scd4x_nodes[i]);

      // Try read
      if (scd4x_read_if_ready(scd4x_nodes[i])) {
        scd4x_fresh[i] = true;
        if (scd4x_tick_start_ms == 0) scd4x_tick_start_ms = millis(); // start on first fresh
      }

      ++i;
    }

    // --- decide if we should commit this window ---
    // How many SCDs are actually present right now?
    size_t scd_present_count = 0;
    for (size_t k=0; k<N_SCD4X; ++k) if (scd4x_nodes[k].present) scd_present_count++;

    // If at least one SCD is present but none have ever gone fresh, start the timeout now
    if (scd_present_count > 0 && scd4x_tick_start_ms == 0) scd4x_tick_start_ms = millis();

    bool all_ready = true;
    for (size_t k=0; k<N_SCD4X; ++k) if (scd4x_nodes[k].present && !scd4x_fresh[k]) { all_ready = false; break; }

    const bool timeout_hit =
        (scd4x_tick_start_ms != 0) &&
        (millis() - scd4x_tick_start_ms >= SCD4X_SYNC_TIMEOUT_MS);

    bool should_commit = false;
    if (scd_present_count > 0) {
      // Normal: wait for all present SCDs, or timeout if one is lagging/broken
      should_commit = all_ready || timeout_hit;
    } else {
      // No connected SCDs at all: commit every 10s using a separate timer
      if (millis() - no_scd_last_commit_ms >= 10000UL) {
        no_scd_last_commit_ms = millis();
        should_commit = true;
      }
    }

    if (should_commit) {
      // (optional) push a sparkline sample right now so graphs move at commit time
      // sample_graphs_now();  // make this call push unconditionally without the time check

      commit_and_reset_all_windows();   // prints Serial, writes CSV, resets all per-sensor RunningAvg and scd4x_fresh[]
      scd4x_tick_start_ms = 0;          // reset window timer
    }
  }


  // --------- T/RH/P (SHT45/TMP117/LPS22DF) ---------
  {
    size_t i = 0;
    for (auto ch : hal::Mux::TRHP) {
      if (!select_channel(Wire, ch, muxStateWire)) { ++i; continue; }

      readings.sht45.valid = false;
      if (sht45_measure(readings.sht45) && readings.sht45.valid) {
        win_trhp_sht45_t[i].add(readings.sht45.temp);
        win_trhp_sht45_rh[i].add(readings.sht45.rh);
      }

      readings.tmp117.valid = false;
      if (tmp117_measure(readings.tmp117) && readings.tmp117.valid) {
        win_trhp_tmp117_t[i].add(readings.tmp117.temp);
      }

      if (lps22df_read_with_autorecover(lps22df_nodes[i])) {
        if (lps22df_nodes[i].p_ready) {
          win_trhp_lps_p[i].add(lps22df_nodes[i].pressure);
        }
        if (lps22df_nodes[i].t_ready) {
          win_trhp_lps_t[i].add(lps22df_nodes[i].temp);
        }
      }

      ++i;
    }
  }

  // --------- TGS2611 ---------
  {
    size_t i = 0;
    for (auto ch : hal::Mux::TGS2611) {
      if (!select_channel(Wire, ch, muxStateWire)) { ++i; continue; }

      readings.ads.valid = false;
      int16_t raw;
      if (ads1113_single_shot(raw)) {
        readings.ads.raw   = raw;
        readings.ads.volts = raw * ADS1113_LSB_V;
        readings.ads.valid = true;
        win_tgs2611_raw[i].add((float)raw);
        win_tgs2611_v[i].add(readings.ads.volts);
      }
      ++i;
    }
  }

  // --------- TGS2616 ---------
  {
    size_t i = 0;
    for (auto ch : hal::Mux::TGS2616) {
      if (!select_channel(Wire, ch, muxStateWire)) { ++i; continue; }

      readings.ads.valid = false;
      int16_t raw;
      if (ads1113_single_shot(raw)) {
        readings.ads.raw   = raw;
        readings.ads.volts = raw * ADS1113_LSB_V;
        readings.ads.valid = true;
        win_tgs2616_raw[i].add((float)raw);
        win_tgs2616_v[i].add(readings.ads.volts);
      }
      ++i;
    }
  }

  sample_graphs_if_due();
  update_oled_if_due();
  delay(100);
}