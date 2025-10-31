// Library includes
#include <Arduino.h>
#include <esp_idf_version.h>   // for ESP_IDF_VERSION / ESP_IDF_VERSION_VAL
#include <Wire.h>
#include <SensirionI2cScd4x.h>
#include <RV-3028-C7.h>          // constiko / RV-3028_C7-Arduino_Library
#include <math.h>
#include <driver/ledc.h>   // ESP-IDF LEDC driver
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
#include "common/calib/tgs_calibration.hpp"
#include "ui/serial_menu.hpp"
#include "net/wifi_manager.hpp"
#include "ui/oled_ui.hpp"

// Namespaces using
using hal::Mux::Ch;

static hal::Mux::Tca9548State muxStateWire;

static ui::OledUi oled;

// ---------- I2C buses ----------
TwoWire WireRTC = TwoWire(1);    // RTC + OLED on I2C1 (GPIO 15/16); sensors stay on default Wire

// ---------- Pins / I2C / MUX ----------

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
    // DD/MM/YYYY HH:MM
    snprintf(out, n, "%02d/%02d/%04d %02d:%02d",
             rtc.getDate(), rtc.getMonth(), y,
             rtc.getHours(), rtc.getMinutes());
  } else {
    snprintf(out, n, "RTC NA");
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

  // Print to Serial ONLY when "Live data" is selected in the menu
  if (ui::live_stream_enabled()) {
    Serial.println(line);
  }

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
bool scd4x_present() { Wire.beginTransmission(hal::I2CAddr::SCD41); return Wire.endTransmission() == 0; }
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
static void lps22df_write(uint8_t reg, uint8_t val) { Wire.beginTransmission(hal::I2CAddr::LPS22DF); Wire.write(reg); Wire.write(val); Wire.endTransmission(); }
static bool lps22df_read_u8(uint8_t reg, uint8_t& v) {
  Wire.beginTransmission(hal::I2CAddr::LPS22DF); Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(hal::I2CAddr::LPS22DF, (uint8_t)1) != 1) return false;
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
  Wire.beginTransmission(hal::I2CAddr::LPS22DF); Wire.write(0x27); Wire.endTransmission(false);
  Wire.requestFrom(hal::I2CAddr::LPS22DF, (uint8_t)1); if (!Wire.available()) return false;
  uint8_t status = Wire.read(); bool got=false;
  if (status & 0x01) {
    Wire.beginTransmission(hal::I2CAddr::LPS22DF); Wire.write(0x28); Wire.endTransmission(false);
    Wire.requestFrom(hal::I2CAddr::LPS22DF, (uint8_t)3);
    if (Wire.available()==3) { uint8_t xl=Wire.read(), l=Wire.read(), h=Wire.read();
      int32_t raw = (int32_t)((h<<16)|(l<<8)|xl); if (raw & 0x800000) raw |= 0xFF000000;
      out.pressure = raw / 4096.0f; out.p_ready=true; got=true; }
  }
  if (status & 0x02) {
    Wire.beginTransmission(hal::I2CAddr::LPS22DF); Wire.write(0x2B); Wire.endTransmission(false);
    Wire.requestFrom(hal::I2CAddr::LPS22DF, (uint8_t)2);
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
static const float    ADS1113_LSB_V = 0.000125f;
bool ads1113_single_shot(int16_t &raw) {
  const uint8_t a = hal::I2CAddr::ADS1113;
  uint16_t cfg = (1u<<15)|(0b100<<12)|(0b001<<9)|(1u<<8)|(0b100<<5)|(0b11);
  Wire.beginTransmission(a); Wire.write(0x01); Wire.write((uint8_t)(cfg>>8)); Wire.write((uint8_t)(cfg&0xFF));
  if (Wire.endTransmission()!=0) return false;
  delay(9);
  Wire.beginTransmission(a); Wire.write(0x00); Wire.endTransmission(false);
  Wire.requestFrom(a, (uint8_t)2); if (Wire.available()!=2) return false;
  raw = (int16_t)((Wire.read()<<8)|Wire.read()); return true;
}

namespace {
  static inline uint8_t to_u8(hal::Mux::Ch ch) { return static_cast<uint8_t>(ch); }
}

void setup() {
  Serial.begin(115200);
  Serial.println("Serial open");

  wifi::begin();
  ui::begin();

  pinMode(LDO_Sensors_EN, OUTPUT);
  digitalWrite(LDO_Sensors_EN, HIGH); // power sensors

  // Sensors I2C (through TCA9548A)
  Wire.begin(SDA, SCL);
  Wire.setClock(100000);
  delay(50);

  // RTC + OLED I2C (direct, no mux) on GPIO 15/16
  WireRTC.begin(RTC_SDA, RTC_SCL);

  bool ok = oled.begin(WireRTC, hal::I2CAddr::SSD1306, /* W */128, /* H */64);
  if (ok) oled.splash(F("Booting..."));

  // IMPORTANT: disable the library's auto-rotate so only our code rotates pages
  oled.setAutoRotate(false);

  oled.setPage(ui::OledUi::Page::CO2);  // keep your chosen start page

  WireRTC.setClock(400000);

  // RTC on WireRTC
  rtc_present = rtc.begin(WireRTC);
  if (rtc_present) {
    Serial.println("RV-3028 RTC ready");
  } else {
    Serial.println("WARNING: RV-3028 not found on RTC bus.");
  }

  // Mux present?
  Wire.beginTransmission(hal::I2CAddr::TCA9548A);
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
  scd4x.begin(Wire, hal::I2CAddr::SCD41);

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

  ui::poll();

  // ---- Page + subpage rotation (match original behavior) ----
  static unsigned long last_step_ms = 0;
  const unsigned long STEP_MS = 2000; // same as SCREEN_PERIOD_MS

  // per-page sub-indexes
  static uint8_t idx_co2 = 0;   // 0..scdN-1 among present SCD4x
  static uint8_t idx_trhp = 0;  // 0..trhpN-1 among present TRHP
  static uint8_t idx_2611 = 0;  // 0..v2611N-1 among present TGS2611
  static uint8_t idx_2616 = 0;  // 0..v2616N-1 among present TGS2616

  if (millis() - last_step_ms >= STEP_MS) {
    last_step_ms = millis();

    // presence-based counts
    uint8_t scdN=0, trhpN=0, v2611N=0, v2616N=0;

    for (size_t i=0;i<N_SCD4X;++i) if (scd4x_nodes[i].present) scdN++;

    for (size_t i=0;i<N_TRHP;++i) {
      bool present =
        lps22df_nodes[i].present ||
        win_trhp_sht45_rh[i].count > 0 ||
        win_trhp_tmp117_t[i].count > 0 ||
        win_trhp_lps_p[i].count    > 0;
      if (present) trhpN++;
    }

    for (size_t i=0;i<N_TGS2611;++i) if (win_tgs2611_v[i].count > 0) v2611N++;
    for (size_t i=0;i<N_TGS2616;++i) if (win_tgs2616_v[i].count > 0) v2616N++;

    // Clamp sub-indexes to the number *present* right now
    if (scdN   && idx_co2  >= scdN)   idx_co2  = 0;
    if (trhpN  && idx_trhp >= trhpN)  idx_trhp = 0;
    if (v2611N && idx_2611 >= v2611N) idx_2611 = 0;
    if (v2616N && idx_2616 >= v2616N) idx_2616 = 0;

    auto cur = oled.currentPage();

    auto advance_to_next_page = [&]() {
      for (uint8_t tries = 0; tries < 8; ++tries) {
        switch (cur) {
          case ui::OledUi::Page::CO2:    cur = ui::OledUi::Page::RH;     break;
          case ui::OledUi::Page::RH:     cur = ui::OledUi::Page::T;      break;
          case ui::OledUi::Page::T:      cur = ui::OledUi::Page::P;      break;
          case ui::OledUi::Page::P:      cur = ui::OledUi::Page::V2611;  break;
          case ui::OledUi::Page::V2611:  cur = ui::OledUi::Page::V2616;  break;
          case ui::OledUi::Page::V2616:  cur = ui::OledUi::Page::CO2;   break;
          default:                       cur = ui::OledUi::Page::CO2;   break;
        }
        bool ok =
          (cur == ui::OledUi::Page::CO2   && scdN)   ||
          (cur == ui::OledUi::Page::RH    && trhpN)  ||
          (cur == ui::OledUi::Page::T     && trhpN)  ||
          (cur == ui::OledUi::Page::P     && trhpN)  ||
          (cur == ui::OledUi::Page::V2611 && v2611N) ||
          (cur == ui::OledUi::Page::V2616 && v2616N);
        if (ok) break;
      }
      oled.setPage(cur);
    };

    // step sub-index for the current page; if wrapped, advance page
    switch (cur) {
      case ui::OledUi::Page::CO2:
        if (scdN) { idx_co2++; if (idx_co2 >= scdN) { idx_co2 = 0; advance_to_next_page(); } }
        else advance_to_next_page();
        break;

      case ui::OledUi::Page::RH:
      case ui::OledUi::Page::T:
      case ui::OledUi::Page::P:
        if (trhpN) { idx_trhp++; if (idx_trhp >= trhpN) { idx_trhp = 0; advance_to_next_page(); } }
        else advance_to_next_page();
        break;

      case ui::OledUi::Page::V2611:
        if (v2611N) { idx_2611++; if (idx_2611 >= v2611N) { idx_2611 = 0; advance_to_next_page(); } }
        else advance_to_next_page();
        break;

      case ui::OledUi::Page::V2616:
        if (v2616N) { idx_2616++; if (idx_2616 >= v2616N) { idx_2616 = 0; advance_to_next_page(); } }
        else advance_to_next_page();
        break;

      default:
        advance_to_next_page();
        break;
    }
  }

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

  // Fill the UI model from your freshest values
  ui::Model m;

  // Clock
  static char clock_buf[24];
  format_timestamp_no_sec(clock_buf, sizeof(clock_buf));
  m.clock_text = clock_buf;

  // Recompute presence-based counts for mapping
  uint8_t scdN=0, trhpN=0, v2611N=0, v2616N=0;
  for (size_t i=0;i<N_SCD4X;++i) if (scd4x_nodes[i].present) scdN++;
  for (size_t i=0;i<N_TRHP;++i) {
    bool present =
        lps22df_nodes[i].present ||
        win_trhp_sht45_rh[i].count > 0 ||
        win_trhp_tmp117_t[i].count > 0 ||
        win_trhp_lps_p[i].count    > 0;
    if (present) trhpN++;
  }
  for (size_t i=0;i<N_TGS2611;++i) if (win_tgs2611_v[i].count > 0) v2611N++;
  for (size_t i=0;i<N_TGS2616;++i) if (win_tgs2616_v[i].count > 0) v2616N++;

  // Build present list (same criterion you already use: window.count > 0)
  {
    uint8_t present[ N_TRHP ];
    uint8_t n = 0;
    for (uint8_t i=0;i<N_TRHP;++i) {
      if (win_trhp_sht45_rh[i].count > 0 || win_trhp_tmp117_t[i].count > 0 || win_trhp_lps_p[i].count > 0) {
        present[n++] = i;
      }
    }
    if (n == 0) {
      oled.setTRHPPhys(0);
      // Header: show NA
      m.rh_idx = m.t_idx = m.p_idx = 0;
      m.rh_n   = m.t_n   = m.p_n   = 0;
      m.sht45_rh = NAN;  m.sht45_rh_fresh = false;
      m.tmp117_t = NAN;  m.tmp117_t_fresh = false;
      m.lps22df_p = NAN; m.lps22df_p_fresh = false;
    } else {
      if (idx_trhp >= n) idx_trhp = 0;
      const uint8_t phys = present[idx_trhp];
      oled.setTRHPPhys(phys);
      // Header values (+fresh) pulled from the same physical slot we’re graphing
      m.rh_idx = m.t_idx = m.p_idx = idx_trhp;   // display index (0-based)
      m.rh_n   = m.t_n   = m.p_n   = n;          // total present
      m.sht45_rh        = (win_trhp_sht45_rh[phys].count > 0) ? (float)win_trhp_sht45_rh[phys].mean() : NAN;
      m.sht45_rh_fresh  = (win_trhp_sht45_rh[phys].count > 0);
      // Choose which temperature you show in the “T” page — here TMP117:
      m.tmp117_t        = (win_trhp_tmp117_t[phys].count > 0) ? (float)win_trhp_tmp117_t[phys].mean() : NAN;
      m.tmp117_t_fresh  = (win_trhp_tmp117_t[phys].count > 0);
      m.lps22df_p       = (win_trhp_lps_p[phys].count > 0) ? (float)win_trhp_lps_p[phys].mean() : NAN;
      m.lps22df_p_fresh = (win_trhp_lps_p[phys].count > 0);
    }
  }

  // --- Decide which physical SCD4x slot maps to CO2 page
  {
    uint8_t present_idxs[N_SCD4X];
    uint8_t n = 0;
    for (uint8_t i = 0; i < N_SCD4X; ++i) {
      if (scd4x_nodes[i].present) present_idxs[n++] = i;
    }
    if (n == 0) {
      oled.setCO2Phys(0);
      m.co2_idx = 0; m.co2_n = 0;
      m.co2_ppm = 0; m.co2_fresh = false;
    } else {
      if (idx_co2 >= n) idx_co2 = 0;
      const uint8_t phys = present_idxs[idx_co2];
      oled.setCO2Phys(phys);
      // Fresh if the node reported recently
      const bool fresh = scd4x_nodes[phys].last_ok_ms &&
                         (millis() - scd4x_nodes[phys].last_ok_ms <= SCD_FRESH_MS);
      m.co2_idx = idx_co2; m.co2_n = n;
      m.co2_ppm = fresh ? (uint16_t)scd4x_nodes[phys].co2 : 0;
      m.co2_fresh = fresh;
    }
  }

  // --- TGS2611
  if (v2611N) {
    uint8_t ord = 0, ch = 0;
    for (; ch < N_TGS2611; ++ch)
      if (win_tgs2611_v[ch].count > 0 && ord++ == idx_2611) break;
    size_t k = (ch < N_TGS2611) ? ch : 0;
    oled.setV2611Phys(k);

    m.tgs2611_v       = win_tgs2611_v[k].mean();
    m.tgs2611_v_fresh = (win_tgs2611_v[k].count > 0);
    m.v2611_idx = idx_2611;
    m.v2611_n   = v2611N;
  }

  // --- TGS2616
  if (v2616N) {
    uint8_t ord = 0, ch = 0;
    for (; ch < N_TGS2616; ++ch)
      if (win_tgs2616_v[ch].count > 0 && ord++ == idx_2616) break;
    size_t k = (ch < N_TGS2616) ? ch : 0;
    oled.setV2616Phys(k);

    m.tgs2616_v       = win_tgs2616_v[k].mean();
    m.tgs2616_v_fresh = (win_tgs2616_v[k].count > 0);
    m.v2616_idx = idx_2616;
    m.v2616_n   = v2616N;
  }

  // --- Adaptive per-channel sparkline sampling (match UI cadence) ---
  {
    static unsigned long last_spark_ms = 0;
    unsigned long now = millis();
    const unsigned long PERIOD = oled.graphSamplePeriodMs();
    if (now - last_spark_ms >= PERIOD) {
      last_spark_ms = now;
      // CO2 (SCD4x) per physical slot: use live node readings + freshness window
      for (uint8_t ch = 0; ch < N_SCD4X; ++ch) {
        const bool fresh = scd4x_nodes[ch].last_ok_ms &&
                           (millis() - scd4x_nodes[ch].last_ok_ms <= SCD_FRESH_MS);
        const float v = fresh ? (float)scd4x_nodes[ch].co2 : 0.0f;
        oled.pushSampleCO2(ch, v, fresh);
      }

      // TRHP per physical slot
      for (uint8_t ch=0; ch<N_TRHP; ++ch) {
        // RH from SHT45
        {
          const bool fresh = (win_trhp_sht45_rh[ch].count > 0);
          const float mean = fresh ? (float)win_trhp_sht45_rh[ch].mean() : 0.0f;
          oled.pushSampleTRHP_RH(ch, mean, fresh);
        }
        // Temperature: choose your displayed source; here we push TMP117 (or use your chosen one)
        {
          const bool fresh = (win_trhp_tmp117_t[ch].count > 0);
          const float mean = fresh ? (float)win_trhp_tmp117_t[ch].mean() : 0.0f;
          oled.pushSampleTRHP_T(ch, mean, fresh);
        }
        // Pressure from LPS
        {
          const bool fresh = (win_trhp_lps_p[ch].count > 0);
          const float mean = fresh ? (float)win_trhp_lps_p[ch].mean() : 0.0f;
          oled.pushSampleTRHP_P(ch, mean, fresh);
        }
      }
      // TGS2611 channels
      for (uint8_t ch = 0; ch < N_TGS2611; ++ch) {
        const bool fresh = (win_tgs2611_v[ch].count > 0);
        const float mean = fresh ? (float)win_tgs2611_v[ch].mean() : 0.0f;
        oled.pushSample2611(ch, mean, fresh);
      }
      // TGS2616 channels
      for (uint8_t ch = 0; ch < N_TGS2616; ++ch) {
        const bool fresh = (win_tgs2616_v[ch].count > 0);
        const float mean = fresh ? (float)win_tgs2616_v[ch].mean() : 0.0f;
        oled.pushSample2616(ch, mean, fresh);
      }
    }
  }

  oled.update(m, STEP_MS);
  delay(100);
}