#include "ui/serial_menu.hpp"
#include "ui/serial_protocol.hpp"
#include "net/wifi_manager.hpp"
#include "logging/sd_logger.hpp"
#include "common/drivers/lora_e5.hpp"
#include <WiFi.h>
#include <vector>
#include <SD_MMC.h>
#include <string.h>  // memcpy

// External function from main.cpp to get RTC time as string
extern void get_rtc_time_string(char* out, size_t n);

// External pump control from main.cpp
extern float pump_percent;
extern void pump_set_percent(float pct);   // raw PWM duty (0..100 %)
extern void pump_apply_output();           // drive PWM for the current switch state
extern bool pump_switch_latched();         // true while the enable switch is latched
void pump_save_percent(float pct);

// External TGS2611 multipoint calibration from main.cpp
// Stores pre-fitted R2ppm (kΩ) and alpha parameters into the sensor EEPROM on channel ch.
extern bool tgs2611_save_calib_params(uint8_t ch, float r2ppm_kohm, float alpha);

// External TGS EEPROM identity write/read from main.cpp (GUI "EEPROM Write" section).
extern bool tgs2611_write_ids(uint8_t ch, uint16_t tgs_id, const char* lot, const char* board_id);
extern bool tgs2611_read_ids(uint8_t ch, uint16_t &tgs_id, bool &crc_ok,
                             char *lot, uint8_t lot_size,
                             char *board_id, uint8_t board_size,
                             uint32_t &calib_epoch, bool &calib_ok);
extern bool tgs2611_clear_ids(uint8_t ch);

// External TGS EEPROM calibration-timestamp write from main.cpp (GUI calibration).
extern bool tgs2611_write_calib_time(uint8_t ch, uint32_t epoch);

// External mainboard ID storage from main.cpp (GUI "Advanced Settings").
extern bool board_id_write(const char* board_id);
extern bool board_id_read(char* out, size_t out_size);
extern bool board_id_clear();

// External firmware version string from main.cpp (GUI "About" section).
extern void firmware_version(char* out, size_t out_size);

namespace ui {

static bool g_live_stream = false;
static bool g_live_just_started = false;
static bool seen_connection = false;
static bool g_busy = false;  // true while processing a command, prevents LIVE_DATA interleaving

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
bool is_busy()             { return g_busy; }

void begin() {
  // Binary protocol doesn't need initialization
}

// Refresh log files from SD card.
// Retries once with a fresh mount so a transient bus error (or a card that was
// re-inserted while the logger had backed off) does not surface as SD_ERROR.
static bool refresh_log_files() {
  g_log_files.clear();

  for (uint8_t attempt = 0; attempt < 2; ++attempt) {
    if (!sd_logger::is_mounted()) {
      // Remount on demand — the hot-plug poll may be mid-backoff.
      if (!sd_logger::try_mount()) return false;
    }

    File dir = SD_MMC.open("/logs");
    if (dir && dir.isDirectory()) {
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
    if (dir) dir.close();

    // Directory unreadable: drop the driver so the retry starts clean.
    sd_logger::unmount();
    delay(50);
  }
  return false;
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
  // Max 255 bytes per chunk — length is sent as a single uint8_t, so 256 would overflow to 0.
  uint8_t buf[255];
  uint32_t remaining = entry.size;
  while (remaining > 0) {
    size_t to_read = (remaining > 255) ? 255 : remaining;
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

  // If in live stream mode, only accept a few commands; EEPROM read/write is
  // allowed so the GUI can program sensor identity without stopping the stream
  // (the command handler below blocks the sensor loop, so mux access is safe).
  bool eeprom_during_live = false;
  uint8_t cmd_byte = 0;
  proto::Cmd cmd = proto::Cmd::BACK;
  if (g_live_stream) {
    if (!Serial.available()) {
      return;
    }
    cmd_byte = Serial.read();
    cmd = (proto::Cmd)cmd_byte;

    if (cmd == proto::Cmd::RTC_TIME) {
      // Allow RTC polling during live stream
      send_rtc_time();
      return;
    } else if (cmd == proto::Cmd::LIVE_STOP) {
      g_live_stream = false;
      proto::write_response(proto::RespType::OK);
      return;
    } else if (cmd == proto::Cmd::EEPROM_READ || cmd == proto::Cmd::EEPROM_WRITE ||
               cmd == proto::Cmd::EEPROM_CLEAR || cmd == proto::Cmd::CALIB_TIMESTAMP ||
               cmd == proto::Cmd::BOARD_ID_WRITE || cmd == proto::Cmd::BOARD_ID_READ ||
               cmd == proto::Cmd::BOARD_ID_CLEAR || cmd == proto::Cmd::FW_VERSION) {
      // Identity programming, calibration timestamp, mainboard ID and version
      // queries are safe mid-stream: they only touch the sensor EEPROM / NVS,
      // and the handler below sets g_busy so the
      // sensor loop cannot interleave a LIVE_DATA frame with the reply.
      eeprom_during_live = true;  // fall through to normal handler below
    } else {
      // Ignore other commands during live streaming
      return;
    }
  }

  if (!eeprom_during_live) {
    // Read command byte
    if (!Serial.available()) {
      return;
    }
    cmd_byte = Serial.read();
    cmd = (proto::Cmd)cmd_byte;
  }

  // Set busy flag for the duration of command processing.
  // This prevents main.cpp's commit_and_reset_all_windows() and header-send
  // from writing LIVE_DATA frames to Serial while we are sending a response.
  g_busy = true;

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
        // Store the speed regardless of the enable switch, so the operator can
        // preset it; pump_apply_output() only drives the pump while latched.
        pump_percent = (float)pct;
        pump_save_percent(pump_percent);
        pump_apply_output();
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::TIMEOUT);
      }
      break;
    }

    case proto::Cmd::PUMP_GET: {
      // Payload: [percent][enabled]; enabled = enable switch latched.
      uint8_t pct = (uint8_t)(pump_percent + 0.5f);
      Serial.write((uint8_t)proto::RespType::PUMP_STATUS);
      Serial.write((uint8_t)2);
      Serial.write(pct);
      Serial.write((uint8_t)(pump_switch_latched() ? 1 : 0));
      break;
    }

    case proto::Cmd::CALIB_MULTIPOINT: {
      // Payload: [ch: u8][r2ppm: 4-byte LE float][alpha: 4-byte LE float]
      // Fitting is done on the GUI side; firmware only stores the coefficients.
      uint8_t ch;
      uint8_t r2ppm_bytes[4], alpha_bytes[4];
      if (!read_byte_timeout(ch) ||
          !read_bytes_timeout(r2ppm_bytes, 4) ||
          !read_bytes_timeout(alpha_bytes, 4)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      float r2ppm, alpha;
      memcpy(&r2ppm, r2ppm_bytes, 4);
      memcpy(&alpha, alpha_bytes, 4);
      if (tgs2611_save_calib_params(ch, r2ppm, alpha)) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    case proto::Cmd::CALIB_TIMESTAMP: {
      // Payload: [ch: u8][unix time: 4-byte LE u32]. Sent automatically by the
      // GUI right after a successful CALIB_MULTIPOINT; stored in the sensor
      // EEPROM so the calibration date can be shown on EEPROM reads.
      uint8_t ch;
      uint8_t ts_bytes[4];
      if (!read_byte_timeout(ch) || !read_bytes_timeout(ts_bytes, 4)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      const uint32_t epoch = uint32_t(ts_bytes[0]) | (uint32_t(ts_bytes[1]) << 8) |
                             (uint32_t(ts_bytes[2]) << 16) | (uint32_t(ts_bytes[3]) << 24);
      if (tgs2611_write_calib_time(ch, epoch)) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    case proto::Cmd::EEPROM_WRITE: {
      // Payload: [ch: u8][tgs_id: u16 BE][lot_len: u8][lot bytes][board_len: u8][board bytes]
      uint8_t ch, id_hi, id_lo, lot_len, board_len;
      if (!read_byte_timeout(ch) || !read_byte_timeout(id_hi) ||
          !read_byte_timeout(id_lo) || !read_byte_timeout(lot_len)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      // Sanity limits before reading variable-length fields
      // (lot: 15-char block, board: 31-char block)
      if (lot_len > 16) {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
        break;
      }
      char lot[17] = {0}, board[33] = {0};
      if (lot_len && !read_bytes_timeout((uint8_t*)lot, lot_len)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      if (!read_byte_timeout(board_len)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      if (board_len > 32) {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
        break;
      }
      if (board_len && !read_bytes_timeout((uint8_t*)board, board_len)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      uint16_t tgs_id = (uint16_t(id_hi) << 8) | id_lo;
      if (tgs2611_write_ids(ch, tgs_id, lot, board)) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    case proto::Cmd::EEPROM_READ: {
      // Payload: [ch: u8]; reply LORA_INFO-style message:
      // "id,crc,lot,board,calib" CSV. calib = decimal unix time of the last
      // calibration, empty when unset/blank, "ERR" when its CRC is bad.
      uint8_t ch;
      if (!read_byte_timeout(ch)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      uint16_t tgs_id = 0; bool crc_ok = false;
      char lot[17] = {0}, board[33] = {0};
      uint32_t calib_epoch = 0; bool calib_ok = false;
      if (!tgs2611_read_ids(ch, tgs_id, crc_ok, lot, sizeof(lot), board, sizeof(board),
                            calib_epoch, calib_ok)) {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
        break;
      }
      char calib_str[12] = "";
      if (calib_ok) {
        if (calib_epoch != 0 && calib_epoch != 0xFFFFFFFFu) {
          snprintf(calib_str, sizeof(calib_str), "%lu", (unsigned long)calib_epoch);
        }
      } else {
        strlcpy(calib_str, "ERR", sizeof(calib_str));
      }
      String payload = String(tgs_id) + "," + (crc_ok ? "OK" : "BAD") + "," + lot + "," +
                       board + "," + calib_str;
      proto::write_message(static_cast<uint8_t>(proto::RespType::EEPROM_IDS), payload);
      break;
    }

    case proto::Cmd::EEPROM_CLEAR: {
      // Payload: [ch: u8]; erases coefficients, calib time and ID/lot/board
      // blocks back to blank 0xFF (full wipe of the sensor EEPROM)
      uint8_t ch;
      if (!read_byte_timeout(ch)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      if (tgs2611_clear_ids(ch)) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    case proto::Cmd::LORA_INFO: {
      String dev_eui = lora::get_dev_eui();
      // get_dev_eui() returns the EUI string, or a diagnostic like
      // "NO_RESP", "PING:...", "PARSE_ERR:..." etc. — pass through as-is.
      if (dev_eui.length() == 0) dev_eui = "READ_ERROR";
      String app_eui = lora::load_app_eui();
      String app_key = lora::load_app_key();
      String payload = dev_eui + "," + app_eui + "," + app_key;
      proto::write_message(static_cast<uint8_t>(proto::RespType::LORA_INFO), payload);
      break;
    }

    case proto::Cmd::LORA_GEN_KEYS: {
      String dev_eui = lora::get_dev_eui();
      if (dev_eui.length() == 0) dev_eui = "READ_ERROR";
      String app_eui = lora::gen_app_eui();
      String app_key = lora::gen_app_key();
      if (!lora::program_keys(app_eui, app_key)) {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
        break;
      }
      lora::save_keys(app_eui, app_key);
      String payload = dev_eui + "," + app_eui + "," + app_key;
      proto::write_message(static_cast<uint8_t>(proto::RespType::LORA_INFO), payload);
      break;
    }

    case proto::Cmd::FW_VERSION: {
      // The firmware version lives on the ESP32 (NVS, seeded from the
      // compile-time constant) so the GUI can compare it against its own
      // version on connect. Payload: length-prefixed version string.
      char ver[32] = {0};
      firmware_version(ver, sizeof(ver));
      proto::write_message(static_cast<uint8_t>(proto::RespType::FW_VERSION),
                           ver, strlen(ver));
      break;
    }

    case proto::Cmd::BOARD_ID_WRITE: {
      // Payload: length-prefixed board ID string (sanitized + capped at 31
      // chars on the firmware side). Stores it in ESP32 NVS namespace "board".
      String board_id;
      if (!read_string(board_id)) {
        proto::write_error(proto::ErrorCode::TIMEOUT);
        break;
      }
      if (board_id_write(board_id.c_str())) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    case proto::Cmd::BOARD_ID_READ: {
      // No payload. Replies BOARD_ID (0x26) with a length-prefixed string;
      // empty string means the mainboard ID has never been set.
      char bid[32] = {0};
      const bool present = board_id_read(bid, sizeof(bid));
      proto::write_message(static_cast<uint8_t>(proto::RespType::BOARD_ID),
                           bid, present ? strlen(bid) : 0);
      break;
    }

    case proto::Cmd::BOARD_ID_CLEAR: {
      // No payload. Erases the mainboard ID from NVS.
      if (board_id_clear()) {
        proto::write_response(proto::RespType::OK);
      } else {
        proto::write_error(proto::ErrorCode::INVALID_CMD);
      }
      break;
    }

    default:
      proto::write_error(proto::ErrorCode::INVALID_CMD);
      break;
  }

  g_busy = false;
}

} // namespace ui