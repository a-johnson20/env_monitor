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

// If the file at `path` does not exist, create it and write `header` + newline.
bool ensure_header(const String& path, const String& header);

// Append one CSV line (no trailing newline required; this function adds it).
bool append_line(const String& path, const String& csv_line);

// Optional helper: card size in MB (0 if unknown / not mounted).
uint64_t card_size_mb();

} // namespace sd_logger
