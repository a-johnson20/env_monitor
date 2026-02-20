#include "ui/serial_menu.hpp"
#include "net/wifi_manager.hpp"
#include <vector>

namespace ui {

enum class State {
  Idle,          // Serial not connected
  MainMenu,
  LiveData,
  WifiMenu,
  WifiScanList,
  WifiSavedList,
  WifiSavedAction, // after picking a saved network
  Prompt          // generic input prompt
};

static State state = State::Idle;
static bool g_live_stream = false;
static bool seen_connection = false;
static bool g_req_download_mode = false;

// simple input buffer
static String inbuf;
static uint32_t last_menu_print_ms = 0;
static int selected_index = -1; // reused in submenus

// helpers
static void print_main_menu() {
  Serial.println();
  Serial.println(F("=== Main Menu ==="));
  Serial.println(F("1) Live data"));
  Serial.println(F("2) WiFi settings"));
  Serial.println(F("3) Download mode"));
  Serial.print(F("> "));
}

// Cache the scan so indices don't shuffle while the user is choosing
static std::vector<wifi::NetInfo> g_scan_cache;

static void print_wifi_menu() {
  Serial.println();
  Serial.println(F("=== WiFi Settings ==="));
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
  // Portable check:
  // - On native USB CDC (ESP32-S3): (bool)Serial is true when a terminal is open.
  // - On UART bridges: Serial is usually always "true", so also check write space.
#if defined(ARDUINO_USB_CDC_ON_BOOT) || defined(USBCON)
  return (bool)Serial;  // true when the USB CDC port is opened by a host
#else
  return (bool)Serial && (Serial.availableForWrite() > 0);
#endif
}

bool live_stream_enabled() { return g_live_stream; }

bool download_mode_requested() { return g_req_download_mode; }
void clear_download_mode_request() { g_req_download_mode = false; }

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
    // Request download mode; main.cpp should stop logging and reboot into MSC.
    g_live_stream = false;
    g_req_download_mode = true;
    Serial.println();
    Serial.println(F("Requesting SD download mode..."));
    Serial.println(F("Stopping logging and rebooting."));
    Serial.println(F("If the SD drive doesn't appear, unplug/replug USB after reboot."));
    return;
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
    g_req_download_mode = false;
    state = State::MainMenu;
    inbuf = "";
    Serial.println(F("\n(Serial connected)"));
    print_main_menu();
  } else if (!connected) {
    if (seen_connection) {
      // lost connection
      seen_connection = false;
      g_live_stream = false;
      g_req_download_mode = false;
      state = State::Idle;
      inbuf = "";
    }
    return;
  }

  //If download was requested, do nothing else; main.cpp should see the flag and reboot.
  if (g_req_download_mode) return;

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
      default: break;
    }
  } else {
    // Periodic reprint in case user attached mid-stream and missed prompt
    if (millis() - last_menu_print_ms > 15000 && (state == State::MainMenu || state == State::WifiMenu)) {
      last_menu_print_ms = millis();
      Serial.print(F("> "));
    }
  }
}

} // namespace ui