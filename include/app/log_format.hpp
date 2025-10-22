#pragma once

#include <Arduino.h>
#include <RV-3028-C7.h>

namespace logfmt {

// Build /logs/YYYYMMDD.csv using the RTC if present, otherwise /logs/nodate.csv
String current_log_path(bool rtc_present, RV3028& rtc);

// Build the CSV header line based on how many sensors/channels are compiled-in.
String make_header(size_t n_scd4x,
                   size_t n_trhp,
                   size_t n_tgs2611,
                   size_t n_tgs2616);

} // namespace logfmt
