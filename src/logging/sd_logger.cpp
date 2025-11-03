#include <SD_MMC.h>

#include "logging/sd_logger.hpp"

namespace {
  // Track mount state locally to this module
  bool g_sd_mounted = false;
  uint32_t g_next_retry_ms = 0;
  constexpr uint32_t RETRY_MS = 2000; // backoff after failures

  // Board-specific pin-out for SDMMC 4-bit
  constexpr int SD_CLK = 12;
  constexpr int SD_CMD = 11;
  constexpr int SD_D0  = 13;
  constexpr int SD_D1  = 14;
  constexpr int SD_D2  = 9;
  constexpr int SD_D3  = 10;
}

namespace sd_logger {

bool begin() {
  SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0, SD_D1, SD_D2, SD_D3);
  if (!SD_MMC.begin("/sdcard", /*mode1bit=*/false, /*formatOnFail=*/false)) {
    Serial.println("SD_MMC mount failed (check wiring/pins).");
    g_sd_mounted = false;
    return false;
  }
  g_sd_mounted = true;
  (void)ensure_dir("/logs");
  Serial.printf("SD card OK: %llu MB\n", SD_MMC.cardSize() / (1024ULL*1024ULL));
  return true;
}

bool is_mounted() { return g_sd_mounted; }

void unmount() {
  SD_MMC.end();
  g_sd_mounted = false;
}

bool try_mount() {
  if (g_sd_mounted) return true;
  SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0, SD_D1, SD_D2, SD_D3);
  if (!SD_MMC.begin("/sdcard", /*mode1bit=*/false, /*formatOnFail=*/false)) {
    return false;
  }
  g_sd_mounted = (SD_MMC.cardType() != CARD_NONE);
  if (!g_sd_mounted) {
    SD_MMC.end();
  }
  return g_sd_mounted;
}

// Call every ~500 ms with millis()
void poll_hotplug(uint32_t now_ms) {
  if (g_sd_mounted) {
    // Cheap probe: if root can't be opened, treat as removal and back off.
    File root = SD_MMC.open("/");
    if (!root) {
      unmount();
      g_next_retry_ms = now_ms + RETRY_MS;
    } else {
      root.close();
    }
    return;
  }
  if (now_ms >= g_next_retry_ms) {
    if (!try_mount()) {
      g_next_retry_ms = now_ms + RETRY_MS;
    }
  }
}

bool ensure_dir(const char* path) {
  if (!g_sd_mounted) return false;
  return SD_MMC.mkdir(path) || SD_MMC.exists(path);
}

bool ensure_header(const String& path, const String& header) {
  if (!g_sd_mounted) return false;
  if (SD_MMC.exists(path)) return true;
  File f = SD_MMC.open(path, FILE_WRITE);
  if (!f) return false;
  f.println(header);
  f.close();
  return true;
}

bool append_line(const String& path, const String& csv_line) {
  if (!g_sd_mounted) return false;
  File f = SD_MMC.open(path, FILE_APPEND);
  if (!f) {
    Serial.println("SD open for append failed");
    unmount();
    g_next_retry_ms = millis() + RETRY_MS;
    return false;
  }
  if (f.println(csv_line) == 0) {
    f.close();
    unmount();
    g_next_retry_ms = millis() + RETRY_MS;
    return false;
  }
  f.close();
  return true;
}

uint64_t card_size_mb() {
  if (!g_sd_mounted) return 0;
  return SD_MMC.cardSize() / (1024ULL*1024ULL);
}

} // namespace sd_logger
