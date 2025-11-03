#pragma once
#include <time.h>
#include <stdlib.h>
#include <string.h>

// Portable replacement for timegm (convert a UTC tm to epoch seconds).
// On platforms that have timegm(), we could call it directly; otherwise
// we temporarily set TZ=UTC and use mktime().
inline time_t timegm_compat(struct tm* tm_utc) {
  // Defensive: null guard
  if (!tm_utc) return (time_t)-1;

  // Try to detect a native timegm at compile time if your toolchain exposes it.
  // (Many ESP/newlib builds don't, so we just use the fallback.)
  // #if defined(_GNU_SOURCE) || defined(__USE_BSD)
  //   return timegm(tm_utc);
  // #else

  // Save current TZ
  const char* old_tz = getenv("TZ");
  char saved[64];
  bool had_old = false;
  if (old_tz) {
    // Keep a small, bounded copy
    strncpy(saved, old_tz, sizeof(saved) - 1);
    saved[sizeof(saved) - 1] = '\0';
    had_old = true;
  }

  // Set TZ to UTC for the conversion
  setenv("TZ", "UTC0", 1);
  tzset();

  time_t t = mktime(tm_utc);  // interpreted as UTC because TZ=UTC0

  // Restore previous TZ
  if (had_old) {
    setenv("TZ", saved, 1);
  } else {
    unsetenv("TZ");
  }
  tzset();

  return t;
  // #endif
}
