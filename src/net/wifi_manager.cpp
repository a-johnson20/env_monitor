#include <WiFi.h>
#include <Preferences.h>
#include <algorithm>

#include "net/wifi_manager.hpp"

extern "C" {
  #include "esp_wifi.h"
  #include "esp_wpa2.h"
}

// ---- WPA2 Enterprise compatibility helpers ----
#ifndef WPA2_CONFIG_INIT_DEFAULT
  #ifdef ESP_WPA2_CONFIG_INIT_DEFAULT
    #define WPA2_CONFIG_INIT_DEFAULT ESP_WPA2_CONFIG_INIT_DEFAULT
  #endif
#endif

namespace wifi {

static Preferences prefs;
static const char* NS = "wifi";
static const char* KEY_COUNT = "count";

static Sec map_auth_mode(wifi_auth_mode_t a) {
  switch (a) {
    case WIFI_AUTH_OPEN:            return Sec::OPEN;
    case WIFI_AUTH_WEP:             return Sec::WEP;
    case WIFI_AUTH_WPA_PSK:         return Sec::WPA_PSK;
    case WIFI_AUTH_WPA2_PSK:        return Sec::WPA2_PSK;
    case WIFI_AUTH_WPA_WPA2_PSK:    return Sec::WPA_WPA2_PSK;
    case WIFI_AUTH_WPA2_ENTERPRISE: return Sec::WPA2_ENTERPRISE;
    default:                        return Sec::UNKNOWN;
  }
}

const char* sec_to_str(Sec s) {
  switch (s) {
    case Sec::OPEN: return "OPEN";
    case Sec::WEP: return "WEP";
    case Sec::WPA_PSK: return "WPA-PSK";
    case Sec::WPA2_PSK: return "WPA2-PSK";
    case Sec::WPA_WPA2_PSK: return "WPA/WPA2-PSK";
    case Sec::WPA2_ENTERPRISE: return "WPA2-ENT";
    default: return "UNKNOWN";
  }
}

void begin() {
  WiFi.mode(WIFI_STA);
  WiFi.persistent(false); // we'll manage our own storage
  prefs.begin(NS, /*readOnly=*/false);
}

std::vector<NetInfo> scan() {
  std::vector<NetInfo> out;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect(); // ensures fresh scan
  int n = WiFi.scanNetworks(/*async=*/false, /*hidden=*/true);
  for (int i=0;i<n;++i) {
    NetInfo ni;
    String ssid;
    uint8_t enc;                // <- uint8_t
    int32_t rssi;
    uint8_t* bssid = nullptr;
    int32_t channel;

    WiFi.getNetworkInfo(i, ssid, enc, rssi, bssid, channel); // <- no 'hidden'

    ni.ssid   = ssid;
    ni.rssi   = rssi;
    ni.channel= (uint8_t)(channel & 0xFF);
    ni.sec    = map_auth_mode((wifi_auth_mode_t)enc); // <- cast to map
    out.push_back(ni);
  }
  // sort by RSSI desc
  std::sort(out.begin(), out.end(), [](const NetInfo&a, const NetInfo&b){ return a.rssi>b.rssi; });
  return out;
}

static bool wait_for_ip(uint32_t timeout_ms) {
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < timeout_ms) {
    delay(100);
  }
  return WiFi.status() == WL_CONNECTED;
}

bool is_connected() {
  return WiFi.status() == WL_CONNECTED;
}

void disconnect() {
  // Clear EAP if enabled
  esp_wifi_sta_wpa2_ent_disable();
  esp_wifi_sta_wpa2_ent_clear_identity();
  esp_wifi_sta_wpa2_ent_clear_username();
  esp_wifi_sta_wpa2_ent_clear_password();
  WiFi.disconnect(/*wifioff=*/false, /*eraseap=*/false);
}

static void save_entry(const Saved& s, int index) {
  String pfx = String("e") + index + "_";
  prefs.putString((pfx + "ssid").c_str(), s.ssid);
  prefs.putUChar((pfx + "sec").c_str(), (uint8_t)s.sec);
  prefs.putString((pfx + "user").c_str(), s.user);
  prefs.putString((pfx + "pass").c_str(), s.pass);
}

static bool load_entry(int index, Saved& out) {
  String pfx = String("e") + index + "_";
  String ssid = prefs.getString((pfx + "ssid").c_str(), "");
  if (!ssid.length()) return false;
  out.ssid = ssid;
  out.sec = (Sec)prefs.getUChar((pfx + "sec").c_str(), (uint8_t)Sec::UNKNOWN);
  out.user = prefs.getString((pfx + "user").c_str(), "");
  out.pass = prefs.getString((pfx + "pass").c_str(), "");
  return true;
}

std::vector<Saved> saved() {
  std::vector<Saved> out;
  int count = (int)prefs.getUInt(KEY_COUNT, 0);
  for (int i=0;i<count;++i) {
    Saved s;
    if (load_entry(i, s)) out.push_back(s);
  }
  return out;
}

bool forget(size_t index) {
  int count = (int)prefs.getUInt(KEY_COUNT, 0);
  if ((int)index < 0 || (int)index >= count) return false;
  // Shuffle down
  for (int i=(int)index; i<count-1; ++i) {
    Saved s;
    load_entry(i+1, s);
    save_entry(s, i);
  }
  // Clear last
  String pfx = String("e") + (count-1) + "_";
  prefs.remove((pfx + "ssid").c_str());
  prefs.remove((pfx + "sec").c_str());
  prefs.remove((pfx + "user").c_str());
  prefs.remove((pfx + "pass").c_str());
  prefs.putUInt(KEY_COUNT, count-1);
  return true;
}

void reset_saved() {
  int count = (int)prefs.getUInt(KEY_COUNT, 0);
  for (int i=0;i<count;++i) {
    String pfx = String("e") + i + "_";
    prefs.remove((pfx + "ssid").c_str());
    prefs.remove((pfx + "sec").c_str());
    prefs.remove((pfx + "user").c_str());
    prefs.remove((pfx + "pass").c_str());
  }
  prefs.putUInt(KEY_COUNT, 0);
}

static void add_or_update_saved(const Saved& s) {
  // if SSID matches, replace; else append
  int count = (int)prefs.getUInt(KEY_COUNT, 0);
  for (int i=0;i<count;++i) {
    Saved cur;
    if (load_entry(i, cur) && cur.ssid == s.ssid) {
      save_entry(s, i);
      return;
    }
  }
  save_entry(s, count);
  prefs.putUInt(KEY_COUNT, count+1);
}

bool connect_psk(const String& ssid, const String& password, bool save, uint32_t timeout_ms) {
  disconnect();
  WiFi.mode(WIFI_STA);
  WiFi.begin(ssid.c_str(), password.c_str());
  if (!wait_for_ip(timeout_ms)) return false;
  if (save) {
    Saved s; s.ssid = ssid; s.sec = Sec::WPA2_PSK; s.pass = password;
    add_or_update_saved(s);
  }
  return true;
}

bool connect_eap_peap_mschapv2(const String& ssid, const String& identity, const String& password, bool save, uint32_t timeout_ms) {
  disconnect();
  WiFi.mode(WIFI_STA);

  // Configure WPA2-Enterprise (PEAP/MSCHAPv2)
  esp_wifi_sta_wpa2_ent_set_identity((uint8_t*)identity.c_str(), identity.length());
  esp_wifi_sta_wpa2_ent_set_username((uint8_t*)identity.c_str(), identity.length());
  esp_wifi_sta_wpa2_ent_set_password((uint8_t*)password.c_str(), password.length());

  // Newer cores: enable() takes a config pointer; older cores: no args
  #ifdef WPA2_CONFIG_INIT_DEFAULT
    esp_wpa2_config_t config = WPA2_CONFIG_INIT_DEFAULT();
    esp_wifi_sta_wpa2_ent_enable(&config);
  #else
    esp_wifi_sta_wpa2_ent_enable();
  #endif

  WiFi.begin(ssid.c_str());
  if (!wait_for_ip(timeout_ms)) {
    // cleanup on failure
    esp_wifi_sta_wpa2_ent_disable();
    return false;
  }

  if (save) {
    Saved s; s.ssid = ssid; s.sec = Sec::WPA2_ENTERPRISE; s.user = identity; s.pass = password;
    add_or_update_saved(s);
  }
  return true;
}

bool connect_saved(size_t index, uint32_t timeout_ms) {
  auto v = saved();
  if (index >= v.size()) return false;
  auto s = v[index];
  if (s.sec == Sec::WPA2_ENTERPRISE) {
    return connect_eap_peap_mschapv2(s.ssid, s.user, s.pass, /*save=*/false, timeout_ms);
  } else {
    return connect_psk(s.ssid, s.pass, /*save=*/false, timeout_ms);
  }
}

} // namespace wifi