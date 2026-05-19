#include "common/drivers/lora_e5.hpp"
#include <HardwareSerial.h>
#include <Preferences.h>
#include <esp_random.h>

namespace lora {

static HardwareSerial& _uart  = Serial2;
static Preferences      _prefs;

// ---------------------------------------------------------------------------
// Internal: send AT command, collect response until "+OK" / "+ERR",
// a custom done_marker, or timeout.  LoRa-E5 read commands do not emit +OK,
// so callers should supply done_marker (first unique token in the response).
// ---------------------------------------------------------------------------
static String at_cmd(const String& cmd, uint32_t timeout_ms = 2000,
                     bool* ok_out = nullptr, const String& done_marker = "") {
    while (_uart.available()) _uart.read();   // flush stale bytes
    _uart.print(cmd);
    _uart.print("\r\n");

    String resp;
    uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
        if (_uart.available()) {
            resp += (char)_uart.read();
            if (resp.indexOf("+OK")  >= 0) { if (ok_out) *ok_out = true;  return resp; }
            if (resp.indexOf("+ERR") >= 0) { if (ok_out) *ok_out = false; return resp; }
            if (done_marker.length() > 0 && resp.indexOf(done_marker) >= 0) {
                // Collect the remainder of the current line (9600 baud: ~2 ms/char)
                delay(30);
                while (_uart.available()) resp += (char)_uart.read();
                if (ok_out) *ok_out = true;
                return resp;
            }
        }
    }
    if (ok_out) *ok_out = false;
    return resp;   // timeout — return whatever arrived
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

void begin() {
    // Configure BOOT pin HIGH before any reset so the module always starts in
    // AT-firmware mode.  PB13 on LoRa-E5: HIGH = AT app, LOW = bootloader.
    pinMode(PIN_LORA_BOOT, OUTPUT);
    digitalWrite(PIN_LORA_BOOT, HIGH);

    _uart.begin(9600, SERIAL_8N1, PIN_LORA_RX, PIN_LORA_TX);

    // --- Try to talk to the module without resetting first (module may already
    //     be running from power-up).
    delay(100);
    while (_uart.available()) _uart.read();  // flush any startup noise
    _uart.print("AT\r\n");
    String probe;
    uint32_t t0 = millis();
    while (millis() - t0 < 800) {
        if (_uart.available()) probe += (char)_uart.read();
        if (probe.indexOf("+OK") >= 0) return;  // already alive — no reset needed
    }

    // No response — pulse RST to force the module into a known state.
    pinMode(PIN_LORA_RST, OUTPUT);
    digitalWrite(PIN_LORA_RST, HIGH);
    delay(5);
    digitalWrite(PIN_LORA_RST, LOW);
    delay(10);
    digitalWrite(PIN_LORA_RST, HIGH);

    // Drain startup noise and wait for the AT firmware to settle (~1.5 s).
    t0 = millis();
    while (millis() - t0 < 1500) {
        while (_uart.available()) _uart.read();
        delay(10);
    }
}

String get_dev_eui() {
    // Send a simple AT ping first to verify the module is alive.
    // LoRa-E5 responds to "AT" with "+AT: OK".
    String ping = at_cmd("AT", 1000, nullptr, "+AT: OK");
    if (ping.indexOf("+AT: OK") < 0) {
        // Return the raw ping response (truncated) so the GUI can show it.
        String dbg = ping;
        dbg.trim();
        if (dbg.length() == 0) return "NO_RESP";
        dbg.replace(",", ";");   // avoid splitting payload in serial_menu
        if (dbg.length() > 20) dbg = dbg.substring(0, 20);
        return "PING:" + dbg;
    }

    // AT+ID=DevEui returns "+ID: DevEui, AA:BB:CC:DD:EE:FF:00:11" with no +OK.
    // Increase delay to 60 ms (EUI line is ~26 bytes @ 9600 baud = ~27 ms).
    String resp = at_cmd("AT+ID=DevEui", 2000, nullptr, "+ID: DevEui,");

    int pos = resp.indexOf("+ID: DevEui,");
    if (pos < 0) {
        String dbg = resp;
        dbg.trim();
        dbg.replace(",", ";");
        if (dbg.length() > 20) dbg = dbg.substring(0, 20);
        return "PARSE_ERR:" + dbg;
    }
    int i = pos + 12;   // skip "+ID: DevEui,"
    while (i < (int)resp.length() && resp[i] == ' ') i++;   // skip spaces
    if (i + 23 > (int)resp.length()) return "SHORT_RESP";
    String eui = resp.substring(i, i + 23);
    eui.trim();
    return (eui.length() == 23) ? eui : "LEN_ERR";
}

bool program_keys(const String& eui, const String& key) {
    // Setting AppEui returns "+ID: AppEui, ..." with no +OK.
    bool ok1 = false;
    at_cmd("AT+ID=AppEui,\"" + eui + "\"", 2000, &ok1, "+ID: AppEui,");
    if (!ok1) return false;

    // Setting AppKey returns "+KEY: APPKEY ..." with no +OK.
    bool ok2 = false;
    at_cmd("AT+KEY=AppKey,\"" + key + "\"", 2000, &ok2, "+KEY: APPKEY");
    return ok2;
}

String gen_app_eui() {
    uint32_t hi = esp_random(), lo = esp_random();
    uint8_t b[8] = {
        (uint8_t)(hi >> 24), (uint8_t)(hi >> 16),
        (uint8_t)(hi >>  8), (uint8_t)(hi),
        (uint8_t)(lo >> 24), (uint8_t)(lo >> 16),
        (uint8_t)(lo >>  8), (uint8_t)(lo)
    };
    char buf[24];
    snprintf(buf, sizeof(buf), "%02X:%02X:%02X:%02X:%02X:%02X:%02X:%02X",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]);
    return String(buf);
}

String gen_app_key() {
    uint32_t w[4] = { esp_random(), esp_random(), esp_random(), esp_random() };
    char buf[33];
    snprintf(buf, sizeof(buf), "%08X%08X%08X%08X", w[0], w[1], w[2], w[3]);
    return String(buf);
}

String load_app_eui() {
    _prefs.begin("lora", /*readOnly=*/true);
    String v = _prefs.getString("app_eui", "");
    _prefs.end();
    return v;
}

String load_app_key() {
    _prefs.begin("lora", /*readOnly=*/true);
    String v = _prefs.getString("app_key", "");
    _prefs.end();
    return v;
}

void save_keys(const String& eui, const String& key) {
    _prefs.begin("lora", /*readOnly=*/false);
    _prefs.putString("app_eui", eui);
    _prefs.putString("app_key", key);
    _prefs.end();
}

bool join(uint32_t timeout_ms) {
    // Configure region and mode (idempotent — safe to repeat)
    at_cmd("AT+DR=EU868",    1000, nullptr, "+DR:");
    at_cmd("AT+CLASS=A",     1000, nullptr, "+CLASS:");
    at_cmd("AT+ADR=ON",      1000, nullptr, "+ADR:");
    at_cmd("AT+MODE=LWOTAA", 1000, nullptr, "+MODE:");

    // Send join request and poll for terminal responses.
    // Note: "+JOIN: Joined already" does NOT produce a trailing "+JOIN: Done",
    // so we handle all cases with raw polling.
    while (_uart.available()) _uart.read();
    _uart.print("AT+JOIN\r\n");
    String resp;
    uint32_t t0 = millis();
    while (millis() - t0 < timeout_ms) {
        if (_uart.available()) resp += (char)_uart.read();
        if (resp.indexOf("+JOIN: Network joined") >= 0) return true;
        if (resp.indexOf("+JOIN: Joined already") >= 0) return true;
        if (resp.indexOf("+JOIN: Join failed")    >= 0) return false;
        if (resp.indexOf("+JOIN: Done")            >= 0) return false;
    }
    return false;  // timeout
}

bool send_hex(const uint8_t* data, size_t len, uint8_t port, uint32_t timeout_ms) {
    at_cmd("AT+PORT=" + String(port), 1000, nullptr, "+PORT:");

    String hex;
    hex.reserve(len * 2);
    for (size_t i = 0; i < len; ++i) {
        char buf[3];
        snprintf(buf, sizeof(buf), "%02X", data[i]);
        hex += buf;
    }
    String resp = at_cmd("AT+MSGHEX=\"" + hex + "\"", timeout_ms, nullptr, "+MSGHEX: Done");
    return resp.indexOf("+MSGHEX: Done") >= 0;
}

} // namespace lora
