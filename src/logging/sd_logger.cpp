#include "logging/sd_logger.hpp"

namespace {
  // Track mount state locally to this module
  bool g_sd_mounted = false;

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
    return false;
  }
  f.println(csv_line);
  f.close();
  return true;
}

uint64_t card_size_mb() {
  if (!g_sd_mounted) return 0;
  return SD_MMC.cardSize() / (1024ULL*1024ULL);
}

} // namespace sd_logger
