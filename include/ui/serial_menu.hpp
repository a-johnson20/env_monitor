#pragma once
#include <Arduino.h>

namespace ui {

// Call once from setup()
void begin();

// Call frequently from loop()
void poll();

// Whether CSV/data streaming should be printed to Serial
bool live_stream_enabled();

// Let the menu know whether Serial is connected.
// This is a best-effort check that works on native USB CDC and UART bridges.
bool serial_connected();

// True if the user selected "SD download mode" from the menu.
bool download_mode_requested();

// Clear the download mode request flag (call after you handle it).
void clear_download_mode_request();

} // namespace ui