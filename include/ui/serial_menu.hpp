#pragma once
#include <Arduino.h>

namespace ui {

// Call once from setup()
void begin();

// Call frequently from loop()
void poll();

// Whether CSV/data streaming should be printed to Serial
bool live_stream_enabled();

// Returns true (once) when live stream was just started — used to force
// an immediate header send even if the 10-second timer hasn't expired.
bool live_just_started();

// Let the menu know whether Serial is connected.
// This is a best-effort check that works on native USB CDC and UART bridges.
bool serial_connected();

// Returns true while the serial menu is processing a command.
// During this time, other code (e.g. commit_and_reset_all_windows) must NOT
// write to Serial to avoid corrupting the binary protocol response.
bool is_busy();

} // namespace ui
