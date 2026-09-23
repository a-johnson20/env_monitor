#pragma once

#include <Arduino.h>

namespace sd_logger {

// Call once in setup(). Returns true on success and prints card size.
bool begin();

// True after successful begin().
bool is_mounted();

// Poll for insert/remove without blocking. Call ~2 Hz from loop().
// Pass millis() so we can rate-limit retries.
void poll_hotplug(uint32_t now_ms);

// Optional helpers used internally and available to callers.
// Returns true if (re)mounted successfully.
bool try_mount();
// Force unmount and clear internal state.
void unmount();

// Ensure a directory exists (e.g., "/logs"). Returns true if exists or created.
bool ensure_dir(const char* path);

// If the file at `path` does not exist, create it and write an optional
// `meta` line (e.g. sensor metadata) followed by `header` + newline.
// The meta line is only written at creation time, so it always sits above
// the heading. Returns true if the file exists or was created.
bool ensure_header(const String& path, const String& header, const String& meta = String());

// True if the file exists on the SD card (false if not mounted).
bool exists(const String& path);

// Append one CSV line (no trailing newline required; this function adds it).
bool append_line(const String& path, const String& csv_line);

void set_paused(bool paused);
bool paused();

// Optional helper: card size in MB (0 if unknown / not mounted).
uint64_t card_size_mb();

} // namespace sd_logger
