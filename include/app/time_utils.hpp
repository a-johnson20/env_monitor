#pragma once
#include <time.h>
#include <RV-3028-C7.h>
#include "app/compat_time.hpp"

// Returns true and fills 'out' with UK-local time when available.
// Prefers system clock (NTP or seeded), falls back to RTC; else returns false.
inline bool now_local_tm(struct tm& out, bool rtc_present, RV3028* rtc) {
  // 1) Prefer system clock if it looks sane (> 2023-11-14)
  time_t now = 0;
  time(&now);
  if (now > 1700000000) {
    return localtime_r(&now, &out) != nullptr;
  }
  // 2) Fallback to RTC (interpret as UTC, then convert to local for formatting)
  if (rtc_present && rtc && rtc->updateTime()) {
    struct tm utc{};
    utc.tm_sec  = rtc->getSeconds();
    utc.tm_min  = rtc->getMinutes();
    utc.tm_hour = rtc->getHours();
    utc.tm_mday = rtc->getDate();
    utc.tm_mon  = rtc->getMonth() - 1;                 // 0..11
    utc.tm_year = (2000 + (rtc->getYear() % 100)) - 1900; // years since 1900

    time_t t = timegm_compat(&utc); // make epoch in UTC
    if (t > 0) {
      return localtime_r(&t, &out) != nullptr;
    }
  }
  return false;
}

// Seed the system clock from the RTC at boot so time() is useful when offline.
// Returns true on success.
inline bool seed_system_clock_from_rtc(RV3028& rtc) {
  if (!rtc.updateTime()) return false;
  struct tm utc{};
  utc.tm_sec  = rtc.getSeconds();
  utc.tm_min  = rtc.getMinutes();
  utc.tm_hour = rtc.getHours();
  utc.tm_mday = rtc.getDate();
  utc.tm_mon  = rtc.getMonth() - 1;
  utc.tm_year = (2000 + (rtc.getYear() % 100)) - 1900;
  time_t t = timegm_compat(&utc);
  if (t <= 0) return false;
  struct timeval tv; tv.tv_sec = t; tv.tv_usec = 0;
  settimeofday(&tv, nullptr);
  return true;
}
