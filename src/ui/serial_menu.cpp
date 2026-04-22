#include "ui/serial_menu.hpp"
#include "ui/serial_protocol.hpp"
#include "net/wifi_manager.hpp"
#include "logging/sd_logger.hpp"
#include <WiFi.h>
#include <vector>
#include <SD_MMC.h>

// External function from main.cpp to get RTC time as string
extern void get_rtc_time_string(char* out, size_t n);

// External pump control from main.cpp
extern float pump_percent;
extern void pump_set_percent(float pct);
void pump_save_percent(float pct);

namespace ui {

static bool g_live_stream = false;
static bool g_live_just_started = false;
static bool seen_connection = false;

struct LogFileEntry {
  String path;
  uint64_t size = 0;
};
static std::vector<LogFileEntry> g_log_files;
static std::vector<wifi::NetInfo> g_scan_cache;
static int g_selected_wifi_index = -1;

// Helper to read a byte with timeout
static bool read_byte_timeout(uint8_t& out, uint32_t timeout_ms = 2000) {
  uint32_t start = millis();
  while (millis() - start < timeout_ms) {
    if (Serial.available()) {
      out = Serial.read();
      return true;
    }
    delay(1);
  }
  return false;
}

// Helper to read N bytes with timeout
static bool read_bytes_timeout(uint8_t* buf, size_t len, uint32_t timeout_ms = 2000) {
  uint32_t start = millis();
  size_t pos = 0;
  while (pos < len && (millis() - start) < timeout_ms) {
    if (Serial.available()) {
      buf[pos++] = Serial.read();
    } else {
      delay(1);
    }
  }
  return pos == len;
}

// Helper to read a string (length byte + data)
static bool read_string(String& out, uint32_t timeout_ms = 2000) {
  uint8_t len;
  if (!read_byte_timeout(len, timeout_ms)) return false;
  if (len == 0) { out = ""; return true; }
  uint8_t buf[256];
  if (!read_bytes_timeout(buf, len, timeout_ms)) return false;
  buf[len] = '\0';
  out = String((char*)buf);
  return true;
}


bool serial_connected() {
  return true;  // Always accept connections
}

bool live_stream_enabled() { return g_live_stream; }
bool live_just_started()   { bool v = g_live_just_started; g_live_just_started = false; return v; }

void begin() {
  // Binary protocol doesn't need initialization
}

// Refresh log files from SD card
static bool refresh_log_files() {
  g_log_files.clear();
  
  File dir = SD_MMC.open("/logs");
  if (!dir || !dir.isDirectory()) {
    if (dir) dir.close();
    return false;
  }

  for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
    if (!f.isDirectory()) {
      LogFileEntry e;
      String path = f.path();
      if (path.length() == 0) {
        path = String("/logs/") + f.name();
      }
      e.path = path;
      e.size = (uint64_t)f.size();
      g_log_files.push_back(e);
    }
    f.close();
  }
  dir.close();
  return true;
}

// Send a list of log files
static void send_log_list() {
  if (!refresh_log_files()) {
    proto::write_error(proto::ErrorCode::SD_ERROR);
    return;
  }
  
  proto::write_response(proto::RespType::LOG_LIST);
  Serial.write((uint8_t)g_log_files.size());
  
  for (size_t i = 0; i < g_log_files.size(); ++i) {
    proto::write_log_entry(g_log_files[i].size, g_log_files[i].path);
  }
}

// Send a log file
static void send_log_file(uint8_t index) {
  if (index >= g_log_files.size()) {
    proto::write_error(proto::ErrorCode::INVALID_INDEX);
    return;
  }
  
  const auto& entry = g_log_files[index];
  File f = SD_MMC.open(entry.path, FILE_READ);
  if (!f || f.isDirectory()) {
    if (f) f.close();
    proto::write_error(proto::ErrorCode::FILE_NOT_FOUND);
    return;
  }

  // Send BEGIN marker
  proto::write_response(proto::RespType::LOG_BEGIN);
  proto::write_log_entry(entry.size, entry.path);
  
  // Send file data in chunks
  uint8_t buf[256];
  uint32_t remaining = entry.size;
  while (remaining > 0) {
    size_t to_read = (remaining > 256) ? 256 : remaining;
    size_t n = f.read(buf, to_read);
    if (n == 0) break;
    
    proto::write_response(proto::RespType::LOG_DATA);
    Serial.write((uint8_t)n);
    Serial.write(buf, n);
    
    remaining -= n;
    delay(0);  // Yield to other tasks
  }
  f.close();
  
  // Send END marker
  proto::write_response(proto::RespType::LOG_END);
  proto::write_log_entry(entry.size, entry.path);
}

// Send WiFi scan results
static void send_wifi_scan() {
  g_scan_cache = wifi::scan();
  
  proto::write_response(proto::RespType::WIFI_LIST);
  Serial.write((uint8_t)g_scan_cache.size());
  
  for (const auto& net : g_scan_cache) {
    uint8_t sec = 0;
    if (net.sec == wifi::Sec::WPA2_ENTERPRISE) sec = 2;
    else if (net.sec == wifi::Sec::WPA_PSK || net.sec == wifi::Sec::WPA2_PSK || net.sec == wifi::Sec::WPA_WPA2_PSK) sec = 1;
    else sec = 0;  // OPEN or other
    
    proto::write_wifi_entry((int8_t)net.rssi, sec, net.ssid);
  }
}

// Send current WiFi connection info
static void send_wifi_status() {
  proto::write_response(proto::RespType::WIFI_INFO);
  
  if (!wifi::is_connected()) {
    Serial.write(0);  // not connected
    return;
  }
  
  Serial.write(1);  // connected
  Serial.write((uint8_t)WiFi.RSSI());
  
  String ssid = WiFi.SSID();
  String ip = WiFi.localIP().toString();
  String gw = WiFi.gatewayIP().toString();
  String dns = WiFi.dnsIP().toString();
  
  proto::write_string(ssid);
  proto::write_string(ip);
  proto::write_string(gw);
  proto::write_string(dns);
}

// Send list of saved WiFi networks
static void send_saved_networks() {
  auto saved = wifi::saved();
  
  proto::write_response(proto::RespType::WIFI_SAVED);
  Serial.write((uint8_t)saved.size());
  
  for (const auto& net : saved) {
    uint8_t sec = 0;
    if (net.sec == wifi::Sec::WPA2_ENTERPRISE) sec = 2;
    else if (net.sec == wifi::Sec::WPA_PSK || net.sec == wifi::Sec::WPA2_PSK || net.sec == wifi::Sec::WPA_WPA2_PSK) sec = 1;
    else sec = 0;
    
    proto::write_wifi_entry(0, sec, net.ssid);  // rssi is not relevant for saved
  }
}

// Send RTC time
static void send_rtc_time() {
  proto::write_response(proto::RespType::RTC_RESPONSE);
  
  char buf[32];
  get_rtc_time_string(buf, sizeof(buf));
  proto::write_string(buf, strlen(buf));
}

// Connect to WiFi (PSK)
static void handle_wifi_connect_psk() {
  String ssid, password;
  
  if (!read_string(ssid)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  if (!read_string(password)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  
  if (wifi::connect_psk(ssid, password, true)) {
    proto::write_response(proto::RespType::OK);
  } else {
    proto::write_error(proto::ErrorCode::WIFI_ERROR);
  }
}

// Connect to WiFi (WPA2-EAP)
static void handle_wifi_connect_eap() {
  String ssid, username, password;
  
  if (!read_string(ssid)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  if (!read_string(username)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  if (!read_string(password)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  
  if (wifi::connect_eap_peap_mschapv2(ssid, username, password, true)) {
    proto::write_response(proto::RespType::OK);
  } else {
    proto::write_error(proto::ErrorCode::WIFI_ERROR);
  }
}

// Connect to a scanned network
static void handle_wifi_connect_from_scan() {
  uint8_t index;
  if (!read_byte_timeout(index)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  
  if (index >= g_scan_cache.size()) {
    proto::write_error(proto::ErrorCode::INVALID_INDEX);
    return;
  }
  
  const auto& net = g_scan_cache[index];
  
  if (net.sec == wifi::Sec::WPA2_ENTERPRISE) {
    String username, password;
    if (!read_string(username)) {
      proto::write_error(proto::ErrorCode::TIMEOUT);
      return;
    }
    if (!read_string(password)) {
      proto::write_error(proto::ErrorCode::TIMEOUT);
      return;
    }
    
    if (wifi::connect_eap_exact(net, username, password, true)) {
      proto::write_response(proto::RespType::OK);
    } else {
      proto::write_error(proto::ErrorCode::WIFI_ERROR);
    }
  } else {
    String password;
    if (net.sec != wifi::Sec::OPEN) {
      if (!read_string(password)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        return;
      }
    }
    
    if (wifi::connect_psk_exact(net, password, true)) {
      proto::write_response(proto::RespType::OK);
    } else {
      proto::write_error(proto::ErrorCode::WIFI_ERROR);
    }
  }
}

// Connect to a saved network
static void handle_wifi_connect_saved() {
  String ssid;
  if (!read_string(ssid)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  
  auto saved = wifi::saved();
  int index = -1;
  for (size_t i = 0; i < saved.size(); i++) {
    if (saved[i].ssid == ssid) {
      index = (int)i;
      break;
    }
  }
  
  if (index < 0) {
    proto::write_error(proto::ErrorCode::INVALID_INDEX);
    return;
  }
  
  if (wifi::connect_saved(index)) {
    proto::write_response(proto::RespType::OK);
  } else {
    proto::write_error(proto::ErrorCode::WIFI_ERROR);
  }
}

// Forget a saved network by SSID
static void handle_wifi_forget() {
  String ssid;
  if (!read_string(ssid)) {
    proto::write_error(proto::ErrorCode::TIMEOUT);
    return;
  }
  
  auto saved_list = wifi::saved();
  int found = -1;
  for (size_t i = 0; i < saved_list.size(); i++) {
    if (saved_list[i].ssid == ssid) {
      found = (int)i;
      break;
    }
  }
  
  if (found < 0) {
    proto::write_error(proto::ErrorCode::INVALID_INDEX);
    return;
  }
  
  if (wifi::forget(found)) {
    proto::write_response(proto::RespType::OK);
  } else {
    proto::write_error(proto::ErrorCode::WIFI_ERROR);
  }
}

// Main command handler
void poll() {
  // Check if serial is connected
  bool connected = serial_connected();
  if (!connected) {
    if (seen_connection) {
      seen_connection = false;
      g_live_stream = false;
    }
    return;
  }

  if (!seen_connection) {
    seen_connection = true;
    g_live_stream = false;
    // Send a welcome message to indicate protocol is ready
    proto::write_response(proto::RespType::OK);
  }

  // If in live stream mode, only check for stop command
  if (g_live_stream) {
    if (Serial.available()) {
      uint8_t cmd_byte = Serial.read();
      proto::Cmd cmd = (proto::Cmd)cmd_byte;
      
      if (cmd == proto::Cmd::RTC_TIME) {
        // Allow RTC polling during live stream
        send_rtc_time();
      } else if (cmd == proto::Cmd::LIVE_STOP) {
        g_live_stream = false;
        proto::write_response(proto::RespType::OK);
      }
      // Ignore other commands during live streaming
    }
    return;
  }

  // Read command byte
  if (!Serial.available()) {
    return;
  }

  uint8_t cmd_byte = Serial.read();
  proto::Cmd cmd = (proto::Cmd)cmd_byte;

  switch (cmd) {
    case proto::Cmd::LIVE_START:
      g_live_stream = true;
      g_live_just_started = true;
      proto::write_response(proto::RespType::OK);
      break;

    case proto::Cmd::LIVE_STOP:
      g_live_stream = false;
      proto::write_response(proto::RespType::OK);
      break;

    case proto::Cmd::WIFI_SCAN:
      send_wifi_scan();
      break;

    case proto::Cmd::WIFI_CONNECT:
      handle_wifi_connect_psk();
      break;

    case proto::Cmd::WIFI_CONNECT_EAP:
      handle_wifi_connect_eap();
      break;

    case proto::Cmd::WIFI_SAVED_LIST:
      send_saved_networks();
      break;

    case proto::Cmd::WIFI_DISCONNECT:
      wifi::disconnect();
      proto::write_response(proto::RespType::OK);
      break;

    case proto::Cmd::WIFI_FORGET:
      handle_wifi_forget();
      break;

    case proto::Cmd::WIFI_CONNECT_SAVED:
      handle_wifi_connect_saved();
      break;

    case proto::Cmd::WIFI_STATUS:
      send_wifi_status();
      break;

    case proto::Cmd::LOG_LIST:
      send_log_list();
      break;

    case proto::Cmd::LOG_GET: {
      uint8_t index;
      if (read_byte_timeout(index)) {
        send_log_file(index);
      } else {
        proto::write_error(proto::ErrorCode::TIMEOUT);
      }
      break;
    }

    case proto::Cmd::RTC_TIME:
      send_rtc_time();
      break;

    case proto::Cmd::PUMP_SET: {
      uint8_t pct;
      if (read_byte_timeout(pct)) {
        if (pct > 100) pct = 100;
        pump_set_percent((float)pct);
        pump_percent = (float)pct;
        pump_save_percent(pump_percent);
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::TIMEOUT);
      }
      break;
    }

    case proto::Cmd::PUMP_GET: {
      uint8_t pct = (uint8_t)(pump_percent + 0.5f);
      Serial.write((uint8_t)proto::RespType::PUMP_STATUS);
      Serial.write((uint8_t)1);
      Serial.write(pct);
      break;
    }

    default:
      proto::write_error(proto::ErrorCode::INVALID_CMD);
      break;
  }
}



} // namespace ui
