#pragma once

#include <Arduino.h>

namespace lora {

// Hardware pins — net names follow the LoRa-E5 module's perspective:
//   "UART_2_RX" net = LoRa module RECEIVES here (PB7) → connected to ESP32 GPIO2 → GPIO2 is ESP32 TX
//   "UART_2_TX" net = LoRa module TRANSMITS here (PB6) → connected to ESP32 GPIO1 → GPIO1 is ESP32 RX
// ESP32 GPIO1  ← LoRa PB6 TX (Serial2 RX)
// ESP32 GPIO2  → LoRa PB7 RX (Serial2 TX)
// ESP32 GPIO8  → LoRa PB13 BOOT (HIGH = AT firmware, LOW = bootloader)
// ESP32 GPIO38 → LoRa RST (active-low)
constexpr int PIN_LORA_RX   = 1;   // GPIO1 receives from LoRa PB6 (UART_2_TX net)
constexpr int PIN_LORA_TX   = 2;   // GPIO2 transmits to  LoRa PB7 (UART_2_RX net)
constexpr int PIN_LORA_BOOT = 8;
constexpr int PIN_LORA_RST  = 38;

// Initialise UART2, configure BOOT/RST GPIO, and reset the module into
// AT-command mode.  Blocks for ~600 ms.  Call once from setup().
void begin();

// Read the Device EUI directly from the module via AT+ID.
// Returns "AA:BB:CC:DD:EE:FF:00:11" on success, "" on failure.
String get_dev_eui();

// Program AppEUI and AppKey into the module via AT commands.
//   eui  — "AA:BB:CC:DD:EE:FF:00:11"  (colon-separated, 23 chars)
//   key  — "AABBCCDDEEFF00112233445566778899"  (32 uppercase hex chars, no separators)
// Returns true on success.
bool program_keys(const String& eui, const String& key);

// Generate 8 cryptographically-random bytes and return as "AA:BB:CC:DD:EE:FF:00:11".
String gen_app_eui();

// Generate 16 cryptographically-random bytes and return as
// "AABBCCDDEEFF00112233445566778899" (32 uppercase hex chars).
String gen_app_key();

// Load stored AppEUI / AppKey from ESP32 NVS (returns "" if not yet set).
String load_app_eui();
String load_app_key();

// Persist AppEUI and AppKey to ESP32 NVS (namespace "lora").
void save_keys(const String& eui, const String& key);

// Configure LoRaWAN OTAA mode (EU868, Class A, ADR on) and attempt a network join.
// Safe to call when already joined — returns true immediately in that case.
// Blocks for up to timeout_ms milliseconds. Returns true on success.
bool join(uint32_t timeout_ms = 15000);

// Send a raw binary payload as an unconfirmed LoRaWAN uplink on FPort `port`.
// Payload is hex-encoded and sent via AT+MSGHEX.
// Returns true when "+MSGHEX: Done" is received within timeout_ms.
bool send_hex(const uint8_t* data, size_t len, uint8_t port = 2, uint32_t timeout_ms = 10000);

} // namespace lora
