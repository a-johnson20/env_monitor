#include <Arduino.h>
#include <time.h>
#include "app/rtc_sync.hpp"
#include "net/wifi_manager.hpp"
#include "app/compat_time.hpp"   // shims for IntelliSense on Windows
#include "ui/serial_protocol.hpp"

namespace rtc_sync {

static constexpr const char* TZ_UK  = "GMT0BST,M3.5.0/1,M10.5.0/2"; // UK DST rules

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

static bool writeRtcFromUtc(const struct tm& utc_tm) {
  if (!g_rtc || !g_rtc_present) return false;

  // Enable 24h mode — returns void, so do it separately.
  g_rtc->set24Hour();

  bool ok = true;
  ok &= static_cast<bool>(g_rtc->setSeconds(utc_tm.tm_sec));
  ok &= static_cast<bool>(g_rtc->setMinutes(utc_tm.tm_min));
  ok &= static_cast<bool>(g_rtc->setHours(utc_tm.tm_hour));
  const uint8_t rtc_wday = (utc_tm.tm_wday == 0) ? 7 : static_cast<uint8_t>(utc_tm.tm_wday); // 1=Mon..7=Sun
  ok &= static_cast<bool>(g_rtc->setWeekday(rtc_wday));
  ok &= static_cast<bool>(g_rtc->setDate(utc_tm.tm_mday));              // 1..31
  ok &= static_cast<bool>(g_rtc->setMonth(utc_tm.tm_mon + 1));          // 1..12
  // RV-3028 library expects full year, e.g. 2026.
  ok &= static_cast<bool>(g_rtc->setYear(static_cast<uint16_t>(utc_tm.tm_year + 1900)));

  return ok;
}


// ---- NTP sync (non-blocking) ----
// The NTP wait used to busy-block loop() for up to 5 s. That stalled the serial
// protocol exactly when a WiFi connection came up (the GUI's WIFI_STATUS polls
// timed out and the UI flashed "WiFi: Disconnected"). The wait is now driven a
// few milliseconds at a time from poll(), keeping the firmware responsive.

static bool     g_sync_pending     = false;   // NTP wait in progress
static uint32_t g_sync_deadline_ms = 0;
static bool     g_sync_result      = false;   // outcome of the most recent sync

// Kick off an NTP fetch. Completion is polled from poll() / syncFromNtp().
static void start_ntp_sync() {
  // NOTE: no TZ switch here — the fetch only uses time()/gmtime_r(), and
  // flipping the global TZ while other loop() code runs (now that the wait is
  // asynchronous) would mis-render timestamps written during the wait.
  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
  g_sync_deadline_ms = millis() + 5000;   // 5s budget (keep well under GUI 15s scan timeout)
  g_sync_pending = true;
}

// Advance a pending NTP wait. Returns true when the wait has finished.
static bool poll_ntp_sync() {
  if (!g_sync_pending) return true;

  time_t now = 0;
  struct tm utc_tm{};
  time(&now);
  if (now > 1700000000 && gmtime_r(&now, &utc_tm) != nullptr) {  // sanity: > 2023-11-14
    g_sync_result = writeRtcFromUtc(utc_tm);
    if (g_sync_result) {
      g_synced_once = true;
      g_last_sync_epoch = static_cast<uint32_t>(now);
    }
    set_tz_uk();  // safety: keep local rendering on UK time
    g_sync_pending = false;
    return true;
  }

  if ((int32_t)(millis() - g_sync_deadline_ms) >= 0) {  // budget expired
    g_sync_result = false;
    set_tz_uk();
    g_sync_pending = false;
    return true;
  }
  return false;  // still waiting — loop() stays responsive
}

// Blocking convenience wrapper (used by force_resync() only).
static bool syncFromNtp() {
  start_ntp_sync();
  while (!poll_ntp_sync()) {
    delay(50);
  }
  return g_sync_result;
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

  // One-shot on connection edge — non-blocking: the NTP wait now completes
  // across poll() calls while loop() keeps running (and answering serial).
  if (w && !g_prev_wifi && !g_synced_once && g_rtc_present && !g_sync_pending) {
    start_ntp_sync();
  }

  // Drive any pending NTP wait forward (a few ms at a time).
  poll_ntp_sync();

  // Daily resync if connected (at most once every ~24h)
  const uint32_t now_ms = millis();
  if (w && g_rtc_present && g_synced_once && !g_sync_pending &&
      (now_ms - g_last_daily_check_ms) >= (24u * 60u * 60u * 1000u)) {
    g_last_daily_check_ms = now_ms;
    start_ntp_sync();
  }

  g_prev_wifi = w;
}

bool force_resync() {
  if (!g_rtc_present) {
    ui::proto::write_status("[RTC] Not present; cannot sync.", 31);
    return false;
  }
  if (!wifi::is_connected()) {
    ui::proto::write_status("[RTC] Wi-Fi not connected; cannot NTP sync.", 44);
    return false;
  }
  const bool ok = syncFromNtp();
  if (ok) ui::proto::write_status("[RTC] Manual resync OK", 22);
  else    ui::proto::write_status("[RTC] Manual resync failed", 26);
  return ok;
}

bool is_synced()            { return g_synced_once; }
uint32_t last_sync_epoch()  { return g_last_sync_epoch; }

} // namespace rtc_sync
