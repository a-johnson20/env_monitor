#pragma once
#include <stdint.h>

// USB Mass Storage Class (MSC) wrapper for SD card export
// Requires: SOC_USB_OTG_SUPPORTED, CONFIG_TINYUSB_MSC_ENABLED, and ARDUINO_USB_MODE=0
// If not available, all functions return false/no-op gracefully

namespace usb_msc_sd {
  bool begin();        // prepare USB MSC hooks (call once in setup)
  bool start();        // switch SD -> MSC
  void stop();         // switch MSC -> SD
  bool active();       // true while SD is exported as MSC

  // Persistent boot-mode switch for reliable MSC enumeration:
  // - true  => reboot into dedicated MSC mode
  // - false => normal firmware mode
  bool set_boot_mode(bool enabled);
  bool boot_mode_enabled();
}
