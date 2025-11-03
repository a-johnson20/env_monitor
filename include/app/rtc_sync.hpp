#pragma once
#include <Arduino.h>
#include <RV-3028-C7.h>

namespace rtc_sync {

// Call in setup() after rtc.begin(...). Pass whether the RTC probed OK.
void begin(RV3028& rtc, bool rtc_present);

// Call in loop(); it syncs once when Wi-Fi comes up and then once daily if connected.
void poll();

// On-demand resync (returns true on success). Only attempts if Wi-Fi is connected.
bool force_resync();

// State helpers
bool is_synced();             // true once we've successfully written the RTC this boot
uint32_t last_sync_epoch();   // seconds since epoch (UTC) of the last successful sync, or 0

} // namespace rtc_sync
