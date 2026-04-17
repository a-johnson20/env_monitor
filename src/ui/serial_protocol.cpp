#include "ui/serial_protocol.hpp"

namespace ui {
namespace proto {

void write_response(RespType type) {
  Serial.write((uint8_t)type);
}

void write_string(const char* str, size_t len) {
  Serial.write((uint8_t)len);
  Serial.write((const uint8_t*)str, len);
}

void write_string(const String& str) {
  write_string(str.c_str(), str.length());
}

void write_wifi_entry(int8_t rssi, uint8_t security, const char* ssid, size_t ssid_len) {
  if (ssid_len > 255) ssid_len = 255;
  Serial.write((uint8_t)rssi);
  Serial.write(security);
  Serial.write((uint8_t)ssid_len);
  Serial.write((const uint8_t*)ssid, ssid_len);
}

void write_wifi_entry(int8_t rssi, uint8_t security, const String& ssid) {
  write_wifi_entry(rssi, security, ssid.c_str(), ssid.length());
}

void write_log_entry(uint32_t size, const char* path, size_t path_len) {
  if (path_len > 255) path_len = 255;
  Serial.write((uint8_t)(size & 0xFF));
  Serial.write((uint8_t)((size >> 8) & 0xFF));
  Serial.write((uint8_t)((size >> 16) & 0xFF));
  Serial.write((uint8_t)((size >> 24) & 0xFF));
  Serial.write((uint8_t)path_len);
  Serial.write((const uint8_t*)path, path_len);
}

void write_log_entry(uint32_t size, const String& path) {
  write_log_entry(size, path.c_str(), path.length());
}

void write_error(ErrorCode code) {
  write_response(RespType::ERROR);
  Serial.write((uint8_t)code);
}

void write_status(const char* msg, size_t len) {
  write_response(RespType::STATUS);
  write_string(msg, len);
}

void write_status(const String& msg) {
  write_status(msg.c_str(), msg.length());
}

} // namespace proto
} // namespace ui
