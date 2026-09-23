#pragma once

#include <Arduino.h>
#include <cstdint>

namespace ui {
namespace proto {

// ============ COMMAND OPCODES (Client -> Device) ============
enum class Cmd : uint8_t {
  // Menu navigation
  MAIN_MENU       = 0x10,  // Enter main menu
  LIVE_START      = 0x11,  // Start live data streaming
  LIVE_STOP       = 0x12,  // Stop live data streaming
  WIFI_MENU       = 0x13,  // Enter WiFi menu
  WIFI_SCAN       = 0x14,  // Scan WiFi networks
  WIFI_CONNECT    = 0x15,  // Connect to WiFi (PSK)
  WIFI_CONNECT_EAP = 0x16, // Connect to WiFi (WPA2-EAP)
  WIFI_SAVED_LIST = 0x17,  // List saved networks
  WIFI_DISCONNECT = 0x18,  // Disconnect WiFi
  WIFI_FORGET     = 0x19,  // Forget network by index
  WIFI_STATUS     = 0x1A,  // Get WiFi status
  WIFI_CONNECT_SAVED = 0x20, // Connect to saved network by SSID
  PUMP_SET        = 0x21,  // Set pump speed (followed by uint8_t percent); only runs while the enable switch is latched
  PUMP_GET        = 0x22,  // Get current pump speed + enable-switch state
  CALIB_MULTIPOINT = 0x23, // Calibrate TGS2611: [ch: u8][r2ppm: 4-byte LE float][alpha: 4-byte LE float]
  CALIB_TIMESTAMP  = 0x2A, // Store calibration date/time in TGS EEPROM: [ch: u8][unix time: 4-byte LE u32]
  LORA_INFO       = 0x24,  // Read LoRa DevEUI + stored AppEUI/AppKey
  LORA_GEN_KEYS   = 0x25,  // Generate new AppEUI + AppKey, store in NVS, program module
  EEPROM_WRITE    = 0x26,  // Write TGS EEPROM IDs: [ch: u8][tgs_id: u16 BE][lot_len: u8][lot][board_len: u8][board]
  EEPROM_READ     = 0x27,  // Read TGS EEPROM IDs: payload [ch: u8]; reply CSV "id,crc,lot,board"
  EEPROM_CLEAR    = 0x28,  // Erase TGS EEPROM (coefficients, calib time, ID/lot/board to blank 0xFF): payload [ch: u8]
  FW_VERSION      = 0x29,  // Get firmware version (reply: FW_VERSION message with version string)
  BOARD_ID_WRITE  = 0x2B,  // Write mainboard ID (length-prefixed string) to ESP32 NVS
  BOARD_ID_READ   = 0x2C,  // Read mainboard ID from ESP32 NVS (reply: BOARD_ID message, length-prefixed)
  BOARD_ID_CLEAR  = 0x2D,  // Erase mainboard ID from ESP32 NVS
  LOG_MENU        = 0x1B,  // Enter log export menu
  LOG_LIST        = 0x1C,  // List log files
  LOG_GET         = 0x1D,  // Get log file (followed by uint8_t index)
  RTC_TIME        = 0x1E,  // Get RTC time
  BACK            = 0x1F,  // Go back/cancel
};

// ============ RESPONSE TYPES (Device -> Client) ============
enum class RespType : uint8_t {
  // Status responses
  OK              = 0x00,  // Success, no payload
  ERROR           = 0x01,  // Error (followed by uint8_t error_code)
  STATUS          = 0x02,  // Status message (followed by length + string)
  
  // Data responses
  WIFI_LIST       = 0x10,  // WiFi scan result list
  WIFI_INFO       = 0x11,  // Current WiFi connection info
  WIFI_SAVED      = 0x12,  // Saved networks list
  LOG_LIST        = 0x13,  // Log files list
  LOG_BEGIN       = 0x14,  // Log file transfer begin (path + size)
  LOG_DATA        = 0x15,  // Log file data chunk
  LOG_END         = 0x16,  // Log file transfer end
  LIVE_DATA       = 0xFE,  // Live sensor data (binary encoded) — 0xFE avoids collision with ASCII space (0x20) in timestamps
  RTC_RESPONSE    = 0x21,  // RTC time response
  PUMP_STATUS     = 0x22,  // Pump status: [uint8_t percent][uint8_t enable switch latched]
  LORA_INFO       = 0x23,  // LoRa identifiers: "DevEUI,AppEUI,AppKey" CSV string
  EEPROM_IDS      = 0x24,  // TGS EEPROM IDs: "id,crc,lot,board" CSV string
  FW_VERSION      = 0x25,  // Firmware version string
  BOARD_ID        = 0x26,  // Mainboard ID (length-prefixed string; empty = not set)
  
  // Prompts
  PROMPT_STRING   = 0x30,  // Request string input (followed by length + prompt_text)
  PROMPT_CONFIRM  = 0x31,  // Request yes/no confirmation
};

// ============ ERROR CODES ============
enum class ErrorCode : uint8_t {
  INVALID_CMD     = 0x01,
  SD_ERROR        = 0x02,
  WIFI_ERROR      = 0x03,
  TIMEOUT         = 0x04,
  INVALID_INDEX   = 0x05,
  NO_NETWORKS     = 0x06,
  NOT_CONNECTED   = 0x07,
  FILE_NOT_FOUND  = 0x08,
};

// ============ PAYLOAD STRUCTURES ============

// WiFi network entry (variable length)
struct WifiNetEntry {
  int8_t rssi;          // Signal strength
  uint8_t security;     // Security type (0=Open, 1=PSK, 2=WPA2-EAP)
  uint8_t ssid_len;
  // char ssid[ssid_len] follows
  
  static constexpr size_t HEADER_SIZE = 3;
  
  size_t total_size() const { return HEADER_SIZE + ssid_len; }
};

// Log file entry (variable length)
struct LogFileEntry {
  uint32_t size;        // File size in bytes
  uint8_t path_len;
  // char path[path_len] follows
  
  static constexpr size_t HEADER_SIZE = 5;
  
  size_t total_size() const { return HEADER_SIZE + path_len; }
};

// WiFi connection info (variable length)
struct WifiConnInfo {
  uint8_t connected;    // 0 = not connected, 1 = connected
  int8_t rssi;          // Signal strength if connected
  uint8_t ssid_len;     // 0 if not connected
  uint8_t ip_len;
  uint8_t gw_len;
  uint8_t dns_len;
  // ssid[ssid_len] + ip[ip_len] + gw[gw_len] + dns[dns_len] follows
};

// ============ HELPER FUNCTIONS ============

// Write a response header
void write_response(RespType type);

// Write a string with length prefix
void write_string(const char* str, size_t len);
void write_string(const String& str);

// Write WiFi network entry
void write_wifi_entry(int8_t rssi, uint8_t security, const char* ssid, size_t ssid_len);
void write_wifi_entry(int8_t rssi, uint8_t security, const String& ssid);

// Write log file entry
void write_log_entry(uint32_t size, const char* path, size_t path_len);
void write_log_entry(uint32_t size, const String& path);

// Write error response
void write_error(ErrorCode code);

// Write status
void write_status(const char* msg, size_t len);
void write_status(const String& msg);

// Write a length-prefixed CSV/text line (for live streaming)
// Format: [1 byte length][N bytes data]
// Note: length is single byte, max 255 bytes
void write_line(const char* str, size_t len);
void write_line(const String& str);

// Write a typed message (for all output: debug, CSV, RTC, etc)
// Format: [1 byte type][1 byte length][N bytes data]
// Types: STATUS (0x02), LIVE_DATA (0xFE), RTC_RESPONSE (0x21), LORA_INFO (0x23)
void write_message(uint8_t type, const char* str, size_t len);
void write_message(uint8_t type, const String& str);

} // namespace proto
} // namespace ui
