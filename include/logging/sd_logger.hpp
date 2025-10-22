#pragma once

#include <Arduino.h>
#include <FS.h>
#include <SD_MMC.h>

namespace sd_logger {

// Call once in setup(). Returns true on success and prints card size.
bool begin();

// True after successful begin().
bool is_mounted();

// Ensure a directory exists (e.g., "/logs"). Returns true if exists or created.
bool ensure_dir(const char* path);

// If the file at `path` does not exist, create it and write `header` + newline.
bool ensure_header(const String& path, const String& header);

// Append one CSV line (no trailing newline required; this function adds it).
bool append_line(const String& path, const String& csv_line);

// Optional helper: card size in MB (0 if unknown / not mounted).
uint64_t card_size_mb();

} // namespace sd_logger
