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

} // namespace ui