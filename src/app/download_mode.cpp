#include <Arduino.h>
#include <Preferences.h>
#include <SD_MMC.h>

#include "USB.h"
#include "USBMSC.h"

#include "app/download_mode.hpp"

namespace {
  constexpr const char* NVS_NS  = "envmon";
  constexpr const char* NVS_KEY = "dl_mode";

  // Must match logging/sd_logger.cpp pinout
  constexpr int SD_CLK = 12;
  constexpr int SD_CMD = 11;
  constexpr int SD_D0  = 13;
  constexpr int SD_D1  = 14;
  constexpr int SD_D2  = 9;
  constexpr int SD_D3  = 10;

  USBMSC g_msc;

  int32_t onWrite(uint32_t lba, uint32_t offset, uint8_t* buffer, uint32_t bufsize) {
    (void)offset;
    const uint32_t secSize = SD_MMC.sectorSize();
    if (!secSize) return 0;
    for (uint32_t i = 0; i < bufsize / secSize; i++) {
      if (!SD_MMC.writeRAW(buffer + (i * secSize), lba + i)) return 0;
    }
    return (int32_t)bufsize;
  }

  int32_t onRead(uint32_t lba, uint32_t offset, void* buffer, uint32_t bufsize) {
    (void)offset;
    const uint32_t secSize = SD_MMC.sectorSize();
    if (!secSize) return 0;
    for (uint32_t i = 0; i < bufsize / secSize; i++) {
      if (!SD_MMC.readRAW((uint8_t*)buffer + (i * secSize), lba + i)) return 0;
    }
    return (int32_t)bufsize;
  }

  bool onStartStop(uint8_t power_condition, bool start, bool load_eject) {
    (void)power_condition; (void)start; (void)load_eject;
    return true;
  }

  [[noreturn]] void enter_msc_forever() {
    Serial.begin(115200);
    delay(200);

    Serial.println("[download] Mounting SD_MMC...");
    SD_MMC.setPins(SD_CLK, SD_CMD, SD_D0, SD_D1, SD_D2, SD_D3);
    if (!SD_MMC.begin("/sdcard", /*mode1bit=*/false, /*formatOnFail=*/false)) {
      Serial.println("[download] SD mount failed. Reboot to recover.");
      while (true) delay(1000);
    }

    const uint32_t sectors = SD_MMC.numSectors();
    const uint16_t secSize = SD_MMC.sectorSize();
    if (sectors == 0 || secSize == 0) {
      Serial.println("[download] SD geometry invalid.");
      while (true) delay(1000);
    }

    Serial.println("[download] Starting USB MSC...");
    g_msc.vendorID("ENVLOG");
    g_msc.productID("SDCARD");
    g_msc.productRevision("1.0");
    g_msc.onRead(onRead);
    g_msc.onWrite(onWrite);
    g_msc.onStartStop(onStartStop);
    g_msc.mediaPresent(true);

    if (!g_msc.begin(sectors, secSize)) {
      Serial.println("[download] MSC begin() failed.");
      while (true) delay(1000);
    }

    USB.begin();
    Serial.println("[download] Ready. SD card should appear as a USB drive on the host.");
    Serial.println("[download] Safely eject on the PC before unplugging.");

    while (true) delay(1000);
  }
}

namespace app::download_mode {

void request_next_boot() {
  Preferences prefs;
  prefs.begin(NVS_NS, false);
  prefs.putBool(NVS_KEY, true);
  prefs.end();
}

void enter_if_flagged() {
  Preferences prefs;
  prefs.begin(NVS_NS, false);
  const bool flagged = prefs.getBool(NVS_KEY, false);
  if (!flagged) {
    prefs.end();
    return;
  }

  // Clear flag so next reboot returns to normal logging mode
  prefs.putBool(NVS_KEY, false);
  prefs.end();

  enter_msc_forever();
}

} // namespace app::download_mode