#include "common/drivers/usb_msc_sd.hpp"

#include <Arduino.h>
#include <SD_MMC.h>

#include <USB.h>
#include <USBMSC.h>

// ESP-IDF low-level SDMMC sector IO
extern "C" {
    #include "driver/sdmmc_host.h"
    #include "sdmmc_cmd.h"
}

#if defined(SOC_USB_OTG_SUPPORTED) && defined(CONFIG_TINYUSB_MSC_ENABLED) && defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 0)
namespace {
  USBMSC g_msc;
  bool g_active = false;
  bool configured = false;
  constexpr uint32_t SECTOR_SIZE = 512;

  struct SDMMCFS_Access : fs::SDMMCFS {
    using fs::SDMMCFS::_card;
  };

  static sdmmc_card_t* get_card() {
    auto* p = reinterpret_cast<SDMMCFS_Access*>(&SD_MMC);
    return p->_card;
  }

  static int32_t onRead(uint32_t lba, uint32_t offset, void* buffer, uint32_t bufsize) {
    sdmmc_card_t* card = get_card();
    if (!card) return -1;
    if (bufsize == 0) return 0;

    // Fast-path for full-sector aligned transfers.
    if ((offset == 0) && ((bufsize % SECTOR_SIZE) == 0)) {
      uint32_t count = bufsize / SECTOR_SIZE;
      return (sdmmc_read_sectors(card, buffer, lba, count) == ESP_OK)
        ? (int32_t)bufsize : -1;
    }

    // Handle partial-sector transfers required by host filesystem probing.
    uint8_t sector[SECTOR_SIZE];
    uint8_t* dst = static_cast<uint8_t*>(buffer);
    uint32_t done = 0;

    while (done < bufsize) {
      uint64_t abs = (uint64_t)lba * SECTOR_SIZE + offset + done;
      uint32_t sec = (uint32_t)(abs / SECTOR_SIZE);
      uint32_t in_sec = (uint32_t)(abs % SECTOR_SIZE);
      uint32_t n = min(SECTOR_SIZE - in_sec, bufsize - done);

      if (sdmmc_read_sectors(card, sector, sec, 1) != ESP_OK) return -1;
      memcpy(dst + done, sector + in_sec, n);
      done += n;
    }

    return (int32_t)bufsize;
  }

  static int32_t onWrite(uint32_t lba, uint32_t offset, uint8_t* buffer, uint32_t bufsize) {
    sdmmc_card_t* card = get_card();
    if (!card) return -1;
    if (bufsize == 0) return 0;

    // Fast-path for full-sector aligned transfers.
    if ((offset == 0) && ((bufsize % SECTOR_SIZE) == 0)) {
      uint32_t count = bufsize / SECTOR_SIZE;
      return (sdmmc_write_sectors(card, buffer, lba, count) == ESP_OK)
        ? (int32_t)bufsize : -1;
    }

    // Handle partial-sector writes via read-modify-write.
    uint8_t sector[SECTOR_SIZE];
    uint32_t done = 0;

    while (done < bufsize) {
      uint64_t abs = (uint64_t)lba * SECTOR_SIZE + offset + done;
      uint32_t sec = (uint32_t)(abs / SECTOR_SIZE);
      uint32_t in_sec = (uint32_t)(abs % SECTOR_SIZE);
      uint32_t n = min(SECTOR_SIZE - in_sec, bufsize - done);

      if (sdmmc_read_sectors(card, sector, sec, 1) != ESP_OK) return -1;
      memcpy(sector + in_sec, buffer + done, n);
      if (sdmmc_write_sectors(card, sector, sec, 1) != ESP_OK) return -1;
      done += n;
    }

    return (int32_t)bufsize;
  }
}
#else
namespace {
  bool g_active = false;
  bool configured = false;
  constexpr uint32_t SECTOR_SIZE = 512;

  struct SDMMCFS_Access : fs::SDMMCFS {
    using fs::SDMMCFS::_card;
  };

  static sdmmc_card_t* get_card() {
    auto* p = reinterpret_cast<SDMMCFS_Access*>(&SD_MMC);
    return p->_card;
  }

  static int32_t onRead(uint32_t /*lba*/, uint32_t /*offset*/, void* /*buffer*/, uint32_t /*bufsize*/) {
    return -1;
  }

  static int32_t onWrite(uint32_t /*lba*/, uint32_t /*offset*/, uint8_t* /*buffer*/, uint32_t /*bufsize*/) {
    return -1;
  }
}
#endif

namespace usb_msc_sd {

namespace {
  // One-shot boot request persisted only across reset (not across power loss).
  RTC_DATA_ATTR uint32_t boot_magic = 0;
  constexpr uint32_t BOOT_MAGIC = 0x4D534331UL;  // "MSC1"
}

bool begin() {
  if (configured) return true;

#if defined(SOC_USB_OTG_SUPPORTED) && defined(CONFIG_TINYUSB_MSC_ENABLED) && defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 0)
  // Identify as a Mass Storage device (still can be composite with CDC)
  g_msc.vendorID("ENV");         // up to 8 chars
  g_msc.productID("SD-MSC");     // up to 16 chars
  g_msc.productRevision("1.0");  // up to 4 chars

  g_msc.onRead(onRead);
  g_msc.onWrite(onWrite);

  // USB stack init is handled in application startup (Serial.begin)
  // earlier; no need to call here.

  configured = true;
  Serial.println("MSC: configured");
  return true;
#else
  (void)onRead; (void)onWrite;
  configured = false;
  Serial.println("MSC: unsupported (requires ARDUINO_USB_MODE=0)");
  return false;
#endif
}

bool start() {
#if defined(SOC_USB_OTG_SUPPORTED) && defined(CONFIG_TINYUSB_MSC_ENABLED) && defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 0)
  if (g_active) return true;

  if (!get_card()) {
    Serial.println("MSC: no SD card handle");
    return false;
  }

  uint64_t bytes = SD_MMC.cardSize();
  if (bytes < SECTOR_SIZE) {
    Serial.println("MSC: invalid SD size");
    return false;
  }

  uint32_t block_count = (uint32_t)(bytes / SECTOR_SIZE);

  g_msc.mediaPresent(true);
  if (!g_msc.begin(block_count, SECTOR_SIZE)) {
    Serial.println("MSC: g_msc.begin failed");
    g_msc.mediaPresent(false);
    return false;
  }

  g_active = true;
  Serial.printf("MSC: active, blocks=%lu size=%lu\n",
                (unsigned long)block_count, (unsigned long)SECTOR_SIZE);
  return true;
#else
  (void)get_card;
  Serial.println("MSC: start unsupported (requires ARDUINO_USB_MODE=0)");
  return false;
#endif
}

void stop() {
  if (!g_active) return;
#if defined(SOC_USB_OTG_SUPPORTED) && defined(CONFIG_TINYUSB_MSC_ENABLED) && defined(ARDUINO_USB_MODE) && (ARDUINO_USB_MODE == 0)
  g_msc.end();
  g_msc.mediaPresent(false);
  g_active = false;
#else
  g_active = false;
#endif
}

bool active() { return g_active; }

bool set_boot_mode(bool enabled) {
  boot_magic = enabled ? BOOT_MAGIC : 0;
  return true;
}

bool boot_mode_enabled() {
  const bool enabled = (boot_magic == BOOT_MAGIC);
  // Consume flag on first read to guarantee one-shot behavior.
  boot_magic = 0;
  return enabled;
}

} // namespace usb_msc_sd
