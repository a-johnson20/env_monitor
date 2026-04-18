#include <WiFi.h>
#include <esp_wifi.h>   // for low-level STA config (BSSID/channel lock)
#include <Preferences.h>
#include <algorithm>
#include <cstring>     //for std::memset/strncpy/memcpy

#include "net/wifi_manager.hpp"
#include "ui/serial_protocol.hpp"

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

  static void log_link_info() {
    // Use STATUS message format instead of raw Serial.printf
    // to avoid corrupting the binary protocol stream
    char buf[128];
    snprintf(buf, sizeof(buf), "[WiFi] SSID=%s  IP=%s  GW=%s  DNS0=%s  RSSI=%d",
      WiFi.SSID().c_str(),
      WiFi.localIP().toString().c_str(),
      WiFi.gatewayIP().toString().c_str(),
      WiFi.dnsIP().toString().c_str(),
      WiFi.RSSI());
    ui::proto::write_status(buf, strlen(buf));
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
    // Note: do NOT call WiFi.disconnect() here — it would drop an active connection.
    // WiFi.scanNetworks() works while connected on ESP32.
    int n = WiFi.scanNetworks(/*async=*/false, /*show_hidden=*/true);

    for (int i = 0; i < n; ++i) {
      NetInfo ni;
      String ssid;
      uint8_t enc;
      int32_t rssi;
      uint8_t* bssid = nullptr;
      int32_t channel;

      WiFi.getNetworkInfo(i, ssid, enc, rssi, bssid, channel);

      ni.ssid   = ssid;
      ni.rssi   = rssi;
      ni.channel= (uint8_t)(channel & 0xFF);
      ni.sec    = map_auth_mode((wifi_auth_mode_t)enc);
      // copy BSSID if present
      if (bssid) {
        for (int j=0;j<6;++j) ni.bssid[j] = bssid[j];
      }
      out.push_back(ni);
    }

    // Sort by strongest first
    std::sort(out.begin(), out.end(),
              [](const NetInfo& a, const NetInfo& b) { return a.rssi > b.rssi; });

    // Collapse duplicates by SSID (keep first = strongest); skip hidden (empty SSID)
    std::vector<NetInfo> dedup;
    dedup.reserve(out.size());
    for (const auto& n : out) {
      if (n.ssid.length() == 0) continue;           // skip hidden
      bool seen = false;
      for (const auto& d : dedup) {
        if (d.ssid == n.ssid) { seen = true; break; }
      }
      if (!seen) dedup.push_back(n);
    }

    return dedup;
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
    esp_wifi_sta_enterprise_disable();
    esp_eap_client_clear_identity();
    esp_eap_client_clear_username();
    esp_eap_client_clear_password();
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
    esp_eap_client_set_identity((uint8_t*)identity.c_str(), identity.length());
    esp_eap_client_set_username((uint8_t*)identity.c_str(), identity.length());
    esp_eap_client_set_password((uint8_t*)password.c_str(), password.length());

    // Newer cores: enable() takes a config pointer; older cores: no args
    #ifdef WPA2_CONFIG_INIT_DEFAULT
      esp_wpa2_config_t config = WPA2_CONFIG_INIT_DEFAULT();
      esp_wifi_sta_wpa2_ent_enable(&config);
    #else
      esp_wifi_sta_enterprise_enable();
    #endif

    WiFi.begin(ssid.c_str());
    if (!wait_for_ip(timeout_ms)) {
      // cleanup on failure
      esp_wifi_sta_enterprise_disable();
      return false;
    }

    if (save) {
      Saved s; s.ssid = ssid; s.sec = Sec::WPA2_ENTERPRISE; s.user = identity; s.pass = password;
      add_or_update_saved(s);
    }
    return true;
  }

  // ===== exact-AP connects (BSSID+channel lock) =====
  bool connect_psk_exact(const NetInfo& ap,
                        const String& password,
                        bool save,
                        uint32_t timeout_ms)
  {
    if (ap.sec == Sec::OPEN) return false; // not a PSK network

    disconnect();
    WiFi.mode(WIFI_STA);

    // Lock to the exact AP to avoid hopping to a different SSID/BSSID
    WiFi.begin(ap.ssid.c_str(),
              password.c_str(),
              (int32_t)ap.channel,
              (const uint8_t*)ap.bssid,
              /*connect=*/true);

    if (!wait_for_ip(timeout_ms)) return false;

    if (save) {
      Saved s; s.ssid = ap.ssid; s.sec = Sec::WPA2_PSK; s.pass = password;
      add_or_update_saved(s);
    }
    return true;
  }

  bool connect_eap_exact(const NetInfo& ap,
                        const String& identity,
                        const String& password,
                        bool save,
                        uint32_t timeout_ms)
  {
    if (ap.sec != Sec::WPA2_ENTERPRISE) return false;

    disconnect();
    WiFi.mode(WIFI_STA);

    // ---- WPA2-Enterprise (PEAP/MSCHAPv2) setup (same as your existing path) ----
    esp_eap_client_set_identity((uint8_t*)identity.c_str(), identity.length());
    esp_eap_client_set_username((uint8_t*)identity.c_str(), identity.length());
    esp_eap_client_set_password((uint8_t*)password.c_str(),  password.length());
  #ifdef WPA2_CONFIG_INIT_DEFAULT
    esp_wpa2_config_t wpa2_cfg = WPA2_CONFIG_INIT_DEFAULT();
    esp_wifi_sta_enterprise_enable(&wpa2_cfg);
  #else
    esp_wifi_sta_enterprise_enable();
  #endif

    // ---- Lock to exact BSSID + channel (your core lacks WiFi.begin(ssid, ch, bssid, ...)) ----
    wifi_config_t cfg{};
    std::memset(&cfg, 0, sizeof(cfg));
    std::strncpy((char*)cfg.sta.ssid, ap.ssid.c_str(), sizeof(cfg.sta.ssid) - 1);
    std::memcpy(cfg.sta.bssid, ap.bssid, 6);
    cfg.sta.bssid_set    = 1;                 // enforce BSSID
    cfg.sta.channel      = ap.channel;        // prefer channel
    cfg.sta.scan_method  = WIFI_FAST_SCAN;    // optional
    cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_ENTERPRISE;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_connect());

    if (!wait_for_ip(timeout_ms)) {
      esp_wifi_sta_enterprise_disable();
      return false;
    }

    if (save) {
      Saved s; s.ssid = ap.ssid; s.sec = Sec::WPA2_ENTERPRISE; s.user = identity; s.pass = password;
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