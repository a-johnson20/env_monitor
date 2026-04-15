#include "ui/serial_menu.hpp"
#include "net/wifi_manager.hpp"
#include "logging/sd_logger.hpp"
#include <WiFi.h>
#include <vector>
#include <SD_MMC.h>

namespace ui {

enum class State {
  Idle,          // Serial not connected
  MainMenu,
  LiveData,
  WifiMenu,
  WifiScanList,
  WifiSavedList,
  WifiSavedAction, // after picking a saved network
  LogExportList,
  Prompt          // generic input prompt
};

static State state = State::Idle;
static bool g_live_stream = false;
static bool seen_connection = false;

// simple input buffer
static String inbuf;
static uint32_t last_menu_print_ms = 0;
static int selected_index = -1; // reused in submenus

struct LogFileEntry {
  String path;
  uint64_t size = 0;
};
static std::vector<LogFileEntry> g_log_files;

// helpers
static void print_main_menu() {
  Serial.println();
  Serial.println(F("=== Main Menu ==="));
  Serial.println(F("1) Live data"));
  Serial.println(F("2) WiFi settings"));
  Serial.println(F("3) Download log file (Serial)"));
  Serial.print(F("> "));
}

// Cache the scan so indices don't shuffle while the user is choosing
static std::vector<wifi::NetInfo> g_scan_cache;

static void print_wifi_menu() {
  Serial.println();
  Serial.println(F("=== WiFi Settings ==="));
  // Print current connection info if connected
  if (wifi::is_connected()) {
    Serial.printf("[WiFi] SSID=%s  IP=%s  GW=%s  DNS0=%s  RSSI=%d\n",
      WiFi.SSID().c_str(),
      WiFi.localIP().toString().c_str(),
      WiFi.gatewayIP().toString().c_str(),
      WiFi.dnsIP().toString().c_str(),
      WiFi.RSSI());
  }
  Serial.println(F("1) Scan networks"));
  Serial.println(F("2) Connect PSK (manual)"));
  Serial.println(F("3) Connect WPA2-EAP (manual)"));
  Serial.println(F("4) Saved networks"));
  Serial.println(F("5) Disconnect"));
  Serial.println(F("6) Reset (forget all)"));
  Serial.println(F("b) Back"));
  Serial.print(F("> "));
}

bool serial_connected() {
  // Do not gate menu visibility on USB "connected/open" semantics.
  // Host CDC signaling (DTR/RTS) is inconsistent across tools/OSes.
  return true;
}

bool live_stream_enabled() { return g_live_stream; }

void begin() {
  inbuf.reserve(64);
  // Don't print here; wait until poll() detects a connection.
}

static bool read_line_nonblocking(String &out) {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      out = inbuf;
      inbuf = "";
      return true;
    }
    // rudimentary backspace handling
    if (c == 0x08 || c == 0x7f) {
      if (inbuf.length()) inbuf.remove(inbuf.length()-1);
      continue;
    }
    inbuf += c;
  }
  return false;
}

static String read_line_blocking(const __FlashStringHelper* prompt) {
  Serial.print(prompt);
  String line;
  while (true) {
    String tmp;
    if (read_line_nonblocking(tmp)) { line = tmp; break; }
    delay(10);
  }
  return line;
}

static void to_upper_inplace(String &s) {
  for (size_t i=0;i<s.length();++i) s[i] = toupper(s[i]);
}

static bool refresh_log_file_list() {
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

static void print_log_export_menu() {
  Serial.println();
  Serial.println(F("=== Log Export (Serial) ==="));

  if (!refresh_log_file_list()) {
    Serial.println(F("Could not read /logs on SD."));
  } else if (g_log_files.empty()) {
    Serial.println(F("No files found in /logs."));
  } else {
    for (size_t i = 0; i < g_log_files.size(); ++i) {
      Serial.print(i + 1);
      Serial.print(F(") "));
      Serial.print(g_log_files[i].path);
      Serial.print(F(" ("));
      Serial.print((unsigned long)g_log_files[i].size);
      Serial.println(F(" bytes)"));
    }
  }

  Serial.println(F("\nChoose file number to stream."));
  Serial.println(F("r) Refresh"));
  Serial.println(F("b) Back"));
  Serial.print(F("> "));
}

static bool stream_log_file(const LogFileEntry& file) {
  File f = SD_MMC.open(file.path, FILE_READ);
  if (!f || f.isDirectory()) {
    if (f) f.close();
    return false;
  }

  Serial.println();
  Serial.print(F("BEGIN_LOG "));
  Serial.print(file.path);
  Serial.print(F(" "));
  Serial.println((unsigned long)file.size);

  uint8_t buf[128];
  size_t sent = 0;
  while (true) {
    size_t n = f.read(buf, sizeof(buf));
    if (n == 0) break;
    Serial.write(buf, n);
    sent += n;
    delay(0);
  }
  f.close();

  Serial.println();
  Serial.print(F("END_LOG "));
  Serial.print(file.path);
  Serial.print(F(" "));
  Serial.println((unsigned long)sent);
  return true;
}

static void handle_log_export_list(const String &line) {
  if (line == "b" || line == "B") {
    sd_logger::set_paused(false);
    state = State::MainMenu;
    print_main_menu();
    return;
  }
  if (line == "r" || line == "R") {
    print_log_export_menu();
    return;
  }

  int sel = line.toInt();
  if (sel <= 0) {
    Serial.println(F("Invalid selection."));
    Serial.print(F("> "));
    return;
  }
  if (sel > (int)g_log_files.size()) {
    Serial.println(F("Out of range."));
    Serial.print(F("> "));
    return;
  }

  const auto& file = g_log_files[(size_t)(sel - 1)];
  Serial.println(F("\nStreaming file over serial..."));
  if (!stream_log_file(file)) {
    Serial.println(F("Stream failed."));
  } else {
    Serial.println(F("\nDone. Use scripts/download_csv.py for automatic CSV save."));
  }
  Serial.print(F("> "));
}

// ---- State handlers ----
static void handle_main_menu(const String &line) {
  if (line == "1") {
    g_live_stream = true;
    state = State::LiveData;
    Serial.println(F("\nStreaming live data. Press 'b' to go back.\n"));
  } else if (line == "2") {
    state = State::WifiMenu;
    print_wifi_menu();
  } else if (line == "3") {
  if (!sd_logger::is_mounted()) {
    Serial.println(F("SD not mounted."));
    print_main_menu();
    return;
  }
  sd_logger::set_paused(true);
  state = State::LogExportList;
  print_log_export_menu();
  } else {
    Serial.println(F("Invalid choice."));
    print_main_menu();
  }
}

static void handle_wifi_menu(const String &line) {
  if (line == "1") {
    // Scan
    Serial.println(F("\nScanning..."));
    g_scan_cache = wifi::scan(); // freeze list until user exits this flow
    if (g_scan_cache.empty()) {
      Serial.println(F("No networks found."));
      print_wifi_menu();
      return;
    }
    Serial.println(F("\n#  RSSI  SEC  SSID"));
    for (size_t i = 0; i < g_scan_cache.size(); ++i) {
      const auto& n = g_scan_cache[i];
      Serial.print(i+1); Serial.print(F(") "));
      Serial.print(n.rssi); Serial.print(F("  "));
      Serial.print(wifi::sec_to_str(n.sec)); Serial.print(F("  "));
      Serial.println(n.ssid);
    }
    Serial.println(F("\nSelect a network number, or 'b' to go back:"));
    Serial.print(F("> "));
    state = State::WifiScanList;
  } else if (line == "2") {
    String ssid = read_line_blocking(F("SSID: "));
    String pass = read_line_blocking(F("Password: "));
    Serial.println(F("Connecting..."));
    if (wifi::connect_psk(ssid, pass, /*save=*/true)) {
      Serial.println(F("Connected. Returning to main menu."));
      state = State::MainMenu;
      print_main_menu();
    } else {
      Serial.println(F("Failed to connect."));
      print_wifi_menu();
    }
  } else if (line == "3") {
    String ssid = read_line_blocking(F("SSID: "));
    String user = read_line_blocking(F("Username/Identity: "));
    String pass = read_line_blocking(F("Password: "));
    Serial.println(F("Connecting (WPA2-Enterprise PEAP/MSCHAPv2)..."));
    if (wifi::connect_eap_peap_mschapv2(ssid, user, pass, /*save=*/true)) {
      Serial.println(F("Connected. Returning to main menu."));
      state = State::MainMenu;
      print_main_menu();
    } else {
      Serial.println(F("Failed to connect."));
      print_wifi_menu();
    }
  } else if (line == "4") {
    auto sv = wifi::saved();
    if (sv.empty()) {
      Serial.println(F("\nNo saved networks."));
      print_wifi_menu();
      return;
    }
    Serial.println(F("\nSaved networks:"));
    for (size_t i=0;i<sv.size();++i) {
      Serial.print(i+1); Serial.print(F(") "));
      Serial.print(sv[i].ssid); Serial.print(F(" [")); Serial.print(wifi::sec_to_str(sv[i].sec)); Serial.println(F("]"));
    }
    Serial.println(F("\nSelect a saved network number, or 'b' to go back:"));
    Serial.print(F("> "));
    state = State::WifiSavedList;
  } else if (line == "5") {
    wifi::disconnect();
    Serial.println(F("Disconnected."));
    print_wifi_menu();
  } else if (line == "6") {
    wifi::reset_saved();
    Serial.println(F("All saved networks deleted."));
    print_wifi_menu();
  } else if (line == "b" || line == "B") {
    state = State::MainMenu;
    print_main_menu();
  } else {
    Serial.println(F("Invalid choice."));
    print_wifi_menu();
  }
}

static void handle_wifi_scan_list(const String &line) {
  if (line == "b" || line == "B") {
    g_scan_cache.clear();
    state = State::WifiMenu;
    print_wifi_menu();
    return;
  }
  int sel = line.toInt();
  if (sel <= 0) {
    Serial.println(F("Invalid selection."));
    Serial.print(F("> "));
    return;
  }
  if (sel > (int)g_scan_cache.size()) {
    Serial.println(F("Out of range."));
    Serial.print(F("> "));
    return;
  }
  auto n = g_scan_cache[sel - 1];
  if (n.sec == wifi::Sec::WPA2_ENTERPRISE) {
    String user = read_line_blocking(F("Username/Identity: "));
    String pass = read_line_blocking(F("Password: "));
    Serial.println(F("Connecting..."));
    if (wifi::connect_eap_exact(n, user, pass, /*save=*/true)) {
      Serial.println(F("Connected. Returning to main menu."));
      state = State::MainMenu;
      print_main_menu();
    } else {
      Serial.println(F("Failed to connect."));
      print_wifi_menu();
      state = State::WifiMenu;
    }
  } else {
    String pass;
    if (n.sec == wifi::Sec::OPEN) {
      // no password
    } else {
      pass = read_line_blocking(F("Password: "));
    }
    Serial.println(F("Connecting..."));
    if (wifi::connect_psk_exact(n, pass, /*save=*/true)) {
      Serial.println(F("Connected. Returning to main menu."));
      state = State::MainMenu;
      print_main_menu();
    } else {
      Serial.println(F("Failed to connect."));
      print_wifi_menu();
      state = State::WifiMenu;
    }
  }
}

static void handle_wifi_saved_list(const String &line) {
  if (line == "b" || line == "B") {
    g_scan_cache.clear();
    state = State::WifiMenu;
    print_wifi_menu();
    return;
  }
  int sel = line.toInt();
  if (sel <= 0) {
    Serial.println(F("Invalid selection."));
    Serial.print(F("> "));
    return;
  }
  auto sv = wifi::saved();
  if (sel > (int)sv.size()) {
    Serial.println(F("Out of range."));
    Serial.print(F("> "));
    return;
  }
  selected_index = sel - 1;
  Serial.println(F("\n1) Connect\n2) Forget\nb) Back"));
  Serial.print(F("> "));
  state = State::WifiSavedAction;
}

static void handle_wifi_saved_action(const String &line) {
  if (line == "1") {
    if (wifi::connect_saved((size_t)selected_index)) {
      Serial.println(F("Connected. Returning to main menu."));
      state = State::MainMenu;
      print_main_menu();
    } else {
      Serial.println(F("Failed to connect."));
      state = State::WifiMenu;
      print_wifi_menu();
    }
  } else if (line == "2") {
    if (wifi::forget((size_t)selected_index)) {
      Serial.println(F("Deleted.\n"));
    } else {
      Serial.println(F("Delete failed.\n"));
    }
    state = State::WifiMenu;
    print_wifi_menu();
  } else if (line == "b" || line == "B") {
    state = State::WifiMenu;
    print_wifi_menu();
  } else {
    Serial.println(F("Invalid choice."));
    Serial.print(F("> "));
  }
}

void poll() {
  // Track connection transitions
  bool connected = serial_connected();
  if (connected && !seen_connection) {
    seen_connection = true;
    g_live_stream = false;
    state = State::MainMenu;
    inbuf = "";
    Serial.println(F("\n(Serial connected)"));
    print_main_menu();
  } else if (!connected) {
    if (seen_connection) {
      // lost connection
      seen_connection = false;
      g_live_stream = false;
      state = State::Idle;
      inbuf = "";
    }
    return;
  }

  // While streaming live data, listen for 'b' to go back
  if (state == State::LiveData) {
    if (Serial.available()) {
      char c = (char)Serial.read();
      if (c == 'b' || c == 'B') {
        g_live_stream = false;
        state = State::MainMenu;
        print_main_menu();
      }
    }
    return; // nothing else to do
  }

  // For menu states, parse full lines
  String line;
  if (read_line_nonblocking(line)) {
    line.trim();
    switch (state) {
      case State::MainMenu:          handle_main_menu(line); break;
      case State::WifiMenu:          handle_wifi_menu(line); break;
      case State::WifiScanList:      handle_wifi_scan_list(line); break;
      case State::WifiSavedList:     handle_wifi_saved_list(line); break;
      case State::WifiSavedAction:   handle_wifi_saved_action(line); break;
      case State::LogExportList:     handle_log_export_list(line); break;
      default: break;
    }
  } else {
    // Periodic reprint in case user attached mid-stream and missed prompt
    if (millis() - last_menu_print_ms > 5000 &&
        (state == State::MainMenu || state == State::WifiMenu || state == State::LogExportList)) {
      last_menu_print_ms = millis();
      if (state == State::MainMenu) {
        print_main_menu();
      } else {
        Serial.print(F("> "));
      }
    }
  }

}

} // namespace ui
