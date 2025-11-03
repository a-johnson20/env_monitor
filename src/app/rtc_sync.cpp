#include <Arduino.h>
#include <time.h>
#include "app/rtc_sync.hpp"
#include "net/wifi_manager.hpp"
#include "app/compat_time.hpp"   // shims for IntelliSense on Windows

namespace rtc_sync {

static constexpr const char* TZ_UK  = "GMT0BST,M3.5.0/1,M10.5.0/2"; // UK DST rules
static constexpr const char* TZ_UTC = "UTC0";

static RV3028*  g_rtc = nullptr;
static bool     g_rtc_present = false;
static bool     g_synced_once = false;
static bool     g_prev_wifi   = false;
static uint32_t g_last_sync_epoch = 0;
static uint32_t g_last_daily_check_ms = 0;

// ---- helpers ----

static inline void set_tz_uk() {
  setenv("TZ", TZ_UK, 1);
  tzset();
}

static inline void set_tz_utc() {
  setenv("TZ", TZ_UTC, 1);
  tzset();
}

static bool writeRtcFromUtc(const struct tm& utc_tm) {
  if (!g_rtc || !g_rtc_present) return false;

  // Enable 24h mode — returns void, so do it separately.
  g_rtc->set24Hour();

  bool ok = true;
  ok &= static_cast<bool>(g_rtc->setSeconds(utc_tm.tm_sec));
  ok &= static_cast<bool>(g_rtc->setMinutes(utc_tm.tm_min));
  ok &= static_cast<bool>(g_rtc->setHours(utc_tm.tm_hour));
  ok &= static_cast<bool>(g_rtc->setWeekday(utc_tm.tm_wday));           // 0=Sun..6=Sat
  ok &= static_cast<bool>(g_rtc->setDate(utc_tm.tm_mday));              // 1..31
  ok &= static_cast<bool>(g_rtc->setMonth(utc_tm.tm_mon + 1));          // 1..12
  ok &= static_cast<bool>(g_rtc->setYear((utc_tm.tm_year + 1900) % 100)); // 00..99

  return ok;
}


static bool syncFromNtp() {
  // Work in UTC while fetching NTP
  set_tz_utc();

  // ESP32 Arduino overload: (gmtOffsetSec, dstOffsetSec, s1, s2, s3)
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");

  const uint32_t deadline = millis() + 15000;   // ~15s budget
  time_t now = 0;
  struct tm utc_tm{};
  bool got = false;
  while (millis() < deadline) {
    time(&now);
    if (now > 1700000000) {                    // sanity: > 2023-11-14
      if (gmtime_r(&now, &utc_tm) != nullptr) {
        got = true;
        break;
      }
    }
    delay(50);
  }
  if (!got) return false;

  if (!writeRtcFromUtc(utc_tm)) return false;

  // --- Verify what the RTC reports (helps catch library year quirks) ---
  if (g_rtc && g_rtc_present) {
    uint8_t r_sec  = g_rtc->getSeconds();
    uint8_t r_min  = g_rtc->getMinutes();
    uint8_t r_hour = g_rtc->getHours();
    uint8_t r_wday = g_rtc->getWeekday();
    uint8_t r_mday = g_rtc->getDate();
    uint8_t r_mon  = g_rtc->getMonth();
    uint8_t r_year = g_rtc->getYear(); // 0..99 expected
    uint16_t rtc_full_year = 2000u + (r_year % 100u);
    Serial.printf("[RTC verify] %04u-%02u-%02u %02u:%02u:%02u (raw yy=%u)\n",
                  rtc_full_year, r_mon, r_mday, r_hour, r_min, r_sec, r_year);
    Serial.printf("[NTP used ] %04d-%02d-%02d %02d:%02d:%02d (UTC)\n",
                  utc_tm.tm_year + 1900, utc_tm.tm_mon + 1, utc_tm.tm_mday,
                  utc_tm.tm_hour, utc_tm.tm_min, utc_tm.tm_sec);
  }

  // Switch process TZ back to UK for UI/logs
  set_tz_uk();

  g_synced_once = true;
  g_last_sync_epoch = static_cast<uint32_t>(now);
  return true;
}

// ---- public API ----

void begin(RV3028& rtc, bool rtc_present) {
  g_rtc = &rtc;
  g_rtc_present = rtc_present;

  // Default local time rendering: UK
  set_tz_uk();

  g_synced_once = false;
  g_prev_wifi = wifi::is_connected();
  g_last_daily_check_ms = millis();
}

void poll() {
  const bool w = wifi::is_connected();

  // One-shot on connection edge
  if (w && !g_prev_wifi && !g_synced_once && g_rtc_present) {
    if (syncFromNtp()) {
      Serial.println("[RTC] NTP -> RV-3028 sync OK");
    } else {
      Serial.println("[RTC] NTP sync failed (will retry on next Wi-Fi connect)");
    }
  }

  // Daily resync if connected (at most once every ~24h)
  const uint32_t now_ms = millis();
  if (w && g_rtc_present && g_synced_once &&
      (now_ms - g_last_daily_check_ms) >= (24u * 60u * 60u * 1000u)) {
    g_last_daily_check_ms = now_ms;
    if (syncFromNtp()) {
      Serial.println("[RTC] Daily resync OK");
    } else {
      Serial.println("[RTC] Daily resync failed");
    }
  }

  g_prev_wifi = w;
}

bool force_resync() {
  if (!g_rtc_present) {
    Serial.println("[RTC] Not present; cannot sync.");
    return false;
  }
  if (!wifi::is_connected()) {
    Serial.println("[RTC] Wi-Fi not connected; cannot NTP sync.");
    return false;
  }
  const bool ok = syncFromNtp();
  Serial.println(ok ? "[RTC] Manual resync OK" : "[RTC] Manual resync failed");
  return ok;
}

bool is_synced()            { return g_synced_once; }
uint32_t last_sync_epoch()  { return g_last_sync_epoch; }

} // namespace rtc_sync
