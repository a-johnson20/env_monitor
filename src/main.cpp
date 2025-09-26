#include <Arduino.h>
#include <esp_idf_version.h>   // for ESP_IDF_VERSION / ESP_IDF_VERSION_VAL
#include <Wire.h>
#include <SensirionI2cScd4x.h>
#include <RV-3028-C7.h>          // constiko / RV-3028_C7-Arduino_Library
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h> // OLED
#include <driver/ledc.h>   // ESP-IDF LEDC driver
#include <math.h> // For sparklines
#include <FS.h>
#include <SD_MMC.h>

// ---------- I2C buses ----------
TwoWire WireRTC = TwoWire(1);    // RTC + OLED on I2C1 (GPIO 15/16); sensors stay on default Wire

// ---------- Pins / I2C / MUX ----------
#define TCA9548A_ADDR   0x70

// Sensor bus (through TCA9548A)
#define SDA             5
#define SCL             6

// RTC + OLED on a separate I2C bus (no mux)
#define RTC_SDA         15
#define RTC_SCL         16

#define LDO_Sensors_EN  41
#define CH_TRHP         0    // SHT45 + TMP117 + LPS22DF
#define CH_SCD4X        1    // SCD41/SCD40
#define CH_TGS2611      4    // TGS2611 with ADS1113 + (future) digipot/EEPROM
#define CH_TGS2616      5    // TGS2616 with ADS1113 + (future) digipot/EEPROM

// ---------- Pump PWM (GPIO21) ----------
#define PUMP_PWM_PIN     21
#define PUMP_PWM_FREQ    1000              // Hz
#define PUMP_PWM_RES     LEDC_TIMER_12_BIT // 12-bit
#define PUMP_LEDC_MODE   LEDC_LOW_SPEED_MODE
#define PUMP_LEDC_TIMER  LEDC_TIMER_0
#define PUMP_LEDC_CH     LEDC_CHANNEL_0

// ---------- OLED ----------
#define OLED_WIDTH   128
#define OLED_HEIGHT   64
#define OLED_ADDR   0x3D
Adafruit_SSD1306 display(OLED_WIDTH, OLED_HEIGHT, &WireRTC, -1);
bool oled_present = false;

// ---- SDMMC (4-bit) pins on ESP32-S3 ----
#define SD_CLK   12
#define SD_CMD   11
#define SD_D0    13
#define SD_D1    14
#define SD_D2     9
#define SD_D3    10

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

struct WindowAverages {
  RunningAvg sht45_t, sht45_rh, tmp117_t, lps22df_t, lps22df_p, tgs2611_raw, tgs2611_v, tgs2616_raw, tgs2616_v;
} win;

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

// One sparkline per screen/metric
Spark hist_co2, hist_sht45_rh, hist_tmp117_t, hist_lps22df_p, hist_tgs2611_v, hist_tgs2616_v;
unsigned long last_graph_sample_ms = 0;

// ---------- Mux state ----------
uint8_t current_channel = 0xFF;
bool select_exclusive(uint8_t ch) {
  if (ch > 7) return false;
  if (current_channel == ch) return true;
  Wire.beginTransmission(TCA9548A_ADDR);
  Wire.write(1 << ch);
  bool ok = (Wire.endTransmission() == 0);
  if (ok) { current_channel = ch; delay(2); }
  return ok;
}

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

// ---- SD logging helpers ----
bool sd_mounted = false;

static void format_date(char* out, size_t n) {
  if (rtc_present && rtc.updateTime()) {
    int y = rtc.getYear(); if (y < 100) y += 2000;
    snprintf(out, n, "%04d%02d%02d", y, rtc.getMonth(), rtc.getDate());
  } else {
    snprintf(out, n, "nodate");
  }
}

static String current_log_path() {
  char d[16];
  format_date(d, sizeof(d));
  String p = "/logs/";
  p += d;
  p += ".csv";
  return p;
}

static void fprint_float_or_na(File& f, float v, int digits) {
  if (isnan(v)) f.print("NA"); else f.print(v, digits);
}

static bool sd_begin() {
  // route SDMMC signals to your pins (needs recent Arduino-ESP32)
  SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0, SD_D1, SD_D2, SD_D3);
  // 4-bit mode = second arg false
  if (!SD_MMC.begin("/sdcard", false, false)) {
    Serial.println("SD_MMC mount failed (check wiring/pins).");
    return false;
  }
  SD_MMC.mkdir("/logs"); // ensure folder exists
  Serial.printf("SD card OK: %llu MB\n", SD_MMC.cardSize() / (1024ULL*1024ULL));
  return (sd_mounted = true);
}

static void sd_ensure_header(const String& path) {
  if (!sd_mounted) return;
  if (!SD_MMC.exists(path)) {
    File f = SD_MMC.open(path, FILE_WRITE);
    if (f) {
      f.println(
        "Timestamp,"
        "CO2,"
        "SCD4x T,"
        "SCD4x RH,"
        "SHT45 T(avg),"
        "SHT45 RH(avg),"
        "TMP117 T(avg),"
        "LPS22DF P(avg),"
        "LPS22DF T(avg),"
        "TGS2611 raw(avg),"
        "TGS2611 V(avg),"
        "TGS2616 raw(avg),"
        "TGS2616 V(avg)"
      );
      f.close();
    }
  }
}

static void sd_append_current_row() {
  if (!sd_mounted) return;

  String path = current_log_path();
  sd_ensure_header(path);

  char ts[24];
  format_timestamp(ts, sizeof(ts));   // "YYYY-MM-DD HH:MM:SS"

  // compute means or NA
  float sht45_t   = win.sht45_t.count   ? win.sht45_t.mean()   : NAN;
  float sht45_rh  = win.sht45_rh.count  ? win.sht45_rh.mean()  : NAN;
  float tmp117_t  = win.tmp117_t.count  ? win.tmp117_t.mean()  : NAN;
  float lps_p     = win.lps22df_p.count ? win.lps22df_p.mean() : NAN;
  float lps_t     = win.lps22df_t.count ? win.lps22df_t.mean() : NAN;
  float tgs2611_v = win.tgs2611_v.count ? win.tgs2611_v.mean() : NAN;
  float tgs2616_v = win.tgs2616_v.count ? win.tgs2616_v.mean() : NAN;
  float scd_t     = readings.scd4x.ready ? readings.scd4x.temp : NAN;
  float scd_rh    = readings.scd4x.ready ? readings.scd4x.rh   : NAN;
  int   tgs2611_r = win.tgs2611_raw.count ? (int)round(win.tgs2611_raw.mean()) : (int)NAN;
  int   tgs2616_r = win.tgs2616_raw.count ? (int)round(win.tgs2616_raw.mean()) : (int)NAN;

  File f = SD_MMC.open(path, FILE_APPEND);
  if (!f) { Serial.println("SD open for append failed"); return; }

  f.print(ts);               f.print(',');
  f.print(readings.scd4x.co2); f.print(',');
  fprint_float_or_na(f, scd_t, 2);  f.print(',');
  fprint_float_or_na(f, scd_rh, 2); f.print(',');
  fprint_float_or_na(f, sht45_t, 2);f.print(',');
  fprint_float_or_na(f, sht45_rh, 2);f.print(',');
  fprint_float_or_na(f, tmp117_t, 2);f.print(',');
  fprint_float_or_na(f, lps_p, 2);  f.print(',');
  fprint_float_or_na(f, lps_t, 2);  f.print(',');
  if (isnan((float)tgs2611_r)) f.print("NA"); else f.print(tgs2611_r); f.print(',');
  fprint_float_or_na(f, tgs2611_v, 5); f.print(',');
  if (isnan((float)tgs2616_r)) f.print("NA"); else f.print(tgs2616_r); f.print(',');
  fprint_float_or_na(f, tgs2616_v, 5);
  f.println();

  f.close(); // flush to card each write
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
  out.co2 = co2; out.temp = t; out.rh = rh; out.ready = true; return true;
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
enum screen_index : uint8_t { SCR_TIME=0, SCR_CO2, SCR_SHT45_RH, SCR_TMP117_T, SCR_LPS22DF_P, SCR_TGS2611_V, SCR_TGS2616_V, SCR_COUNT };
unsigned long last_screen_ms = 0;
const unsigned long SCREEN_PERIOD_MS = 2000;
uint8_t screen_index = SCR_TIME;

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
    case SCR_TIME:
    format_timestamp_no_sec(line, sizeof(line));
    oled_show_text("Date & Time", line);
    break;

    case SCR_CO2: {
      char line[24];
      if (readings.scd4x.ready) snprintf(line, sizeof(line), "%u ppm", readings.scd4x.co2);
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("SCD41 CO2", line, hist_co2);
      break;
    }

    case SCR_SHT45_RH: {
      char line[24];
      if (win.sht45_rh.count) snprintf(line, sizeof(line), "%.2f %%", win.sht45_rh.mean());
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("SHT45 RH", line, hist_sht45_rh);
      break;
    }

    case SCR_TMP117_T: {
      char line[24];
      if (win.tmp117_t.count) snprintf(line, sizeof(line), "%.2f C", win.tmp117_t.mean());
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("TMP117 temp", line, hist_tmp117_t);
      break;
    }

    case SCR_LPS22DF_P: {
      char line[24];
      if (win.lps22df_p.count) snprintf(line, sizeof(line), "%.2f hPa", win.lps22df_p.mean());
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("LPS22DF pressure", line, hist_lps22df_p);
      break;
    }

    case SCR_TGS2611_V: {
      char line[24];
      if (win.tgs2611_v.count) snprintf(line, sizeof(line), "%.5f V", win.tgs2611_v.mean());
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("TGS2611 CH4", line, hist_tgs2611_v);
      break;
    }

    case SCR_TGS2616_V: {
      char line[24];
      if (win.tgs2616_v.count) snprintf(line, sizeof(line), "%.5f V", win.tgs2616_v.mean());
      else snprintf(line, sizeof(line), "NA");
      oled_show_value_and_graph("TGS2616 H2", line, hist_tgs2616_v);
      break;
    }

  }
  screen_index = (screen_index + 1) % SCR_COUNT;
}

static void sample_graphs_if_due() {
  unsigned long now = millis();
  if (now - last_graph_sample_ms < GRAPH_SAMPLE_MS) return;
  last_graph_sample_ms = now;

  // Use current window means (nice smoothing). If no data → push invalid.
  hist_co2.push(           readings.scd4x.co2,            readings.scd4x.ready);
  hist_sht45_rh.push(      win.sht45_rh.mean(),           win.sht45_rh.count > 0);
  hist_tmp117_t.push(      win.tmp117_t.mean(),           win.tmp117_t.count > 0);
  hist_lps22df_p.push(     win.lps22df_p.mean(),          win.lps22df_p.count > 0);
  hist_tgs2611_v.push(     win.tgs2611_v.mean(),          win.tgs2611_v.count > 0);
  hist_tgs2616_v.push(     win.tgs2616_v.mean(),          win.tgs2616_v.count > 0);
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

  // Bind SCD4x driver and fetch serial (on CH_SCD4X)
  scd4x.begin(Wire, SCD41_I2C_ADDR_62);
  if (select_exclusive(CH_SCD4X)) {
    uint64_t sn = 0;
    if (scd4x.getSerialNumber(sn) == NO_ERROR) {
      Serial.print("SCD4x serial: ");
      Serial.print((uint32_t)(sn >> 32), HEX);
      Serial.println((uint32_t)(sn & 0xFFFFFFFF), HEX);
    }
  }

  // Init LPS22DF on its own channel
  if (select_exclusive(CH_TRHP)) {
    if (!lps22df_begin_on_selected(readings.lps22df)) {
      Serial.println("LPS22DF init failed");
    }
  }

  if (!sd_begin()) {
    Serial.println("WARNING: SD logging disabled.");
  }

  pump_begin();
  pump_set_percent(50.0f);  // start at 50% (pick what you want)
}

void loop() {
  // --------- SCD4x on channel 1 ---------
  if (select_exclusive(CH_SCD4X)) {
    scd4x_ensure_running(readings.scd4x);

    // When SCD4x has a fresh sample (~every 5 s), print it + the averaged window,
    // then clear the window accumulators.
    if (scd4x_read_if_ready(readings.scd4x)) {
      // Single, combined line with timestamp + all readings
      print_timestamp();
      Serial.print("CO2: ");     Serial.print(readings.scd4x.co2);      Serial.print("ppm, ");
      Serial.print("SCD4x T: "); Serial.print(readings.scd4x.temp, 2);  Serial.print("°C, ");
      Serial.print("SCD4x RH: ");Serial.print(readings.scd4x.rh, 2);    Serial.print("%, ");

      // Window averages since previous SCD4x read
      print_avg_kv("SHT45 T(avg): ",    win.sht45_t,   2, "°C");
      print_avg_kv("SHT45 RH(avg): ",   win.sht45_rh,  2, "%");
      print_avg_kv("TMP117 T(avg): ",   win.tmp117_t,  2, "°C");
      print_avg_kv("LPS22DF P(avg): ",  win.lps22df_p, 2, "hPa");
      print_avg_kv("LPS22DF T(avg): ",  win.lps22df_t, 2, "°C");
      print_avg_kv("TGS2611 raw(avg): ",    win.tgs2611_raw,   0, "", true);
      print_avg_kv("TGS2611 V(avg): ",      win.tgs2611_v,     5, "V", false);
      print_avg_kv("TGS2616 raw(avg): ",    win.tgs2616_raw,   0, "", true);
      print_avg_kv("TGS2616 V(avg): ",      win.tgs2616_v,     5, "V", false);
      Serial.println();

      sd_append_current_row(); // Write to SD card

      // Reset window for the next ~5 s span
      win.sht45_t.reset(); 
      win.sht45_rh.reset(); 
      win.tmp117_t.reset();
      win.lps22df_p.reset(); 
      win.lps22df_t.reset(); 
      win.tgs2611_raw.reset(); 
      win.tgs2611_v.reset();
      win.tgs2616_raw.reset(); 
      win.tgs2616_v.reset();
    }
  }

  // --------- T/RH/P on channel 0 ---------
  if (select_exclusive(CH_TRHP)) {
    readings.sht45.valid = false;
    if (sht45_measure(readings.sht45) && readings.sht45.valid) {
      win.sht45_t.add(readings.sht45.temp);
      win.sht45_rh.add(readings.sht45.rh);
    }

    readings.tmp117.valid = false;
    if (tmp117_measure(readings.tmp117) && readings.tmp117.valid) {
      win.tmp117_t.add(readings.tmp117.temp);
    }

    if (lps22df_read_with_autorecover(readings.lps22df)) {
      if (readings.lps22df.p_ready) win.lps22df_p.add(readings.lps22df.pressure);
      if (readings.lps22df.t_ready) win.lps22df_t.add(readings.lps22df.temp);
    }
  }

  // --------- TGS2611 on channel 4 ---------
  if (select_exclusive(CH_TGS2611)) {
    readings.ads.valid = false;
    int16_t raw;
    if (ads1113_single_shot(raw)) {
      readings.ads.raw = raw;
      readings.ads.volts = raw * ADS1113_LSB_V;
      readings.ads.valid = true;
      win.tgs2611_raw.add((float)raw);
      win.tgs2611_v.add(readings.ads.volts);
    }
  }

  // --------- TGS2616 on channel 5 ---------
  if (select_exclusive(CH_TGS2616)) {
    readings.ads.valid = false;
    int16_t raw;
    if (ads1113_single_shot(raw)) {
      readings.ads.raw = raw;
      readings.ads.volts = raw * ADS1113_LSB_V;
      readings.ads.valid = true;
      win.tgs2616_raw.add((float)raw);
      win.tgs2616_v.add(readings.ads.volts);
    }
  }

  sample_graphs_if_due();
  // OLED rotates every 2 seconds with latest available values
  update_oled_if_due();

  delay(100); // light pacing
}
