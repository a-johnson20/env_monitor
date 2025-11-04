#pragma once

// These shims silence IntelliSense on Windows while remaining no-ops on ESP32 builds.
// They only activate when MSVC headers are in play (the usual case for VS Code on Windows).
#if defined(_WIN32) && !defined(ARDUINO) && !defined(ESP_PLATFORM)
  #include <time.h>
  #include <stdlib.h>

  // Map POSIX gmtime_r/localtime_r to the MSVC-secure variants for IntelliSense.
  inline struct tm* gmtime_r(const time_t* timep, struct tm* result) {
    return (gmtime_s(result, timep) == 0) ? result : nullptr;
  }
  inline struct tm* localtime_r(const time_t* timep, struct tm* result) {
    return (localtime_s(result, timep) == 0) ? result : nullptr;
  }

  // Provide a setenv shim for IntelliSense (uses _putenv_s under the hood).
  inline int setenv(const char* name, const char* value, int) {
    return _putenv_s(name, value);
  }

  // Map tzset to MSVC name so IntelliSense knows it.
  #define tzset _tzset
#endif
