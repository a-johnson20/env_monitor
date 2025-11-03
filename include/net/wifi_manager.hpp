#pragma once
#include <Arduino.h>
#include <vector>

namespace wifi {

enum class Sec {
  OPEN, WEP, WPA_PSK, WPA2_PSK, WPA_WPA2_PSK, WPA2_ENTERPRISE, UNKNOWN
};

struct NetInfo {
  String ssid;
  int32_t rssi = 0;
  Sec sec = Sec::UNKNOWN;
  uint8_t channel = 0;
  uint8_t bssid[6] = {0,0,0,0,0,0}; // exact AP identity for lock
};

struct Saved {
  String ssid;
  Sec sec = Sec::UNKNOWN;
  String user; // for EAP
  String pass; // for PSK or EAP
};

// Convert Sec to human string
const char* sec_to_str(Sec s);

// Call from setup() once
void begin();

// Scan for networks
std::vector<NetInfo> scan();

// Connect helpers (returns true on success). Optionally save credentials.
bool connect_psk(const String& ssid, const String& password, bool save = true, uint32_t timeout_ms = 15000);
bool connect_eap_peap_mschapv2(const String& ssid, const String& identity, const String& password, bool save = true, uint32_t timeout_ms = 20000);

// connect to an EXACT AP (BSSID+channel lock) using a previously scanned NetInfo
bool connect_psk_exact(const NetInfo& ap, const String& password, bool save = true, uint32_t timeout_ms = 15000);
bool connect_eap_exact(const NetInfo& ap, const String& identity, const String& password, bool save = true, uint32_t timeout_ms = 20000);

// Saved networks
std::vector<Saved> saved();
bool connect_saved(size_t index, uint32_t timeout_ms = 15000);
bool forget(size_t index);
void reset_saved();

// Connection control
bool is_connected();
void disconnect();

} // namespace wifi