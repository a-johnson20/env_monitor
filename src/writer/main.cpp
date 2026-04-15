#include <Arduino.h>
#include <Wire.h>

#include "hal/i2c_addresses.hpp"
#include "hal/tca9548a.hpp"
#include "common/tgs_lookup_tables.hpp"
#include "common/eeprom/at24_11.hpp"
#include "common/calib/tgs_calibration.hpp"

/* ================== Pins / Addresses (adjust if needed) ================== */
#define I2C_SDA        5
#define I2C_SCL        6
#define TCA_ADDR       0x70          // default TCA9548A mux address
#define LDO_SENS_EN    41            // sensor rail enable (if applicable)
#define TCA_RESET      3

/* ================== Mux helpers ================== */
static uint8_t g_tcaAddr = 0;

static bool i2c_present(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

static bool tcaSelect(uint8_t ch) {
  if (g_tcaAddr == 0) return false;
  Wire.beginTransmission(g_tcaAddr);
  Wire.write(1 << ch);
  if (Wire.endTransmission() != 0) return false;
  delay(2);                          // small settle
  return true;
}

static uint8_t detect_tca_addr() {
  for (uint8_t addr = 0x70; addr <= 0x77; ++addr) {
    if (i2c_present(addr)) return addr;
  }
  return 0;
}

static bool tcaPresent() {
  if (g_tcaAddr != 0 && hal::Mux::probe(Wire, g_tcaAddr)) return true;
  g_tcaAddr = detect_tca_addr();
  return g_tcaAddr != 0;
}

static void tcaOff() {
  if (g_tcaAddr == 0) return;
  Wire.beginTransmission(g_tcaAddr);
  Wire.write(0);
  Wire.endTransmission();
}

/* ================== I2C scan (on selected channel) ================== */
static void i2c_scan_channel(uint8_t ch) {
  uint8_t found = 0;
  Serial.printf("Scanning mux channel %u...\n", ch);
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("I2C: 0x%02X\n", a);
      ++found;
    }
  }
  if (found == 0) {
    Serial.println("No I2C devices responded on this channel.");
    Serial.println("Expected TGS2611 module devices are typically 0x48, 0x50, and sometimes 0x28.");
  }
}

static void scan_main_bus() {
  bool found = false;
  for (uint8_t a = 1; a < 127; ++a) {
    if (i2c_present(a)) {
      Serial.printf("BUS: 0x%02X\n", a);
      found = true;
    }
  }
  if (!found) Serial.println("BUS: no I2C devices responded");
}

/* ================== ID block (0x00..0x02) ================== */
static bool write_id(uint16_t id){
  uint8_t buf[3] = { uint8_t(id>>8), uint8_t(id&0xFF), 0 };
  buf[2] = crc8_xor(buf, 2);
  if (!at24_write(hal::I2CAddr::AT24, 0x00, buf, 3)) return false;

  // verify
  uint8_t rb[3] = {0};
  if (!at24_read(hal::I2CAddr::AT24, 0x00, rb, 3)) return false;
  uint16_t rid = (uint16_t(rb[0])<<8) | rb[1];
  return (rid == id) && (crc8_xor(rb, 2) == rb[2]);
}

static bool read_id(uint16_t &out_id, bool &crc_ok){
  uint8_t rb[3] = {0};
  if (!at24_read(hal::I2CAddr::AT24, 0x00, rb, 3)) return false;
  out_id = (uint16_t(rb[0])<<8) | rb[1];
  crc_ok = (crc8_xor(rb, 2) == rb[2]);
  return true;
}

/* ================== Lot# block (separate, backward-compatible) ================== */
/* Layout:
   0x20: len (0..15)
   0x21..0x2F: ASCII payload (up to 15 chars, rest zero-padded)
   0x30: CRC8 over [len + payload[0..len-1]]
*/
#define LOT_OFF       0x20
#define LOT_MAX_LEN   15
#define LOT_DATA_OFF  (LOT_OFF + 1)
#define LOT_CRC_OFF   (LOT_OFF + 1 + LOT_MAX_LEN)

static uint8_t sanitize_lot(const char* in, char* out) {
  // allow A-Z 0-9 '-' ; convert to uppercase, drop others
  uint8_t n = 0;
  while (*in && n < LOT_MAX_LEN) {
    char c = *in++;
    if (c >= 'a' && c <= 'z') c -= 32;
    if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c=='-') {
      out[n++] = c;
    }
  }
  return n;
}

static bool write_lot(const char* lot_str) {
  char payload[LOT_MAX_LEN];
  uint8_t len = sanitize_lot(lot_str, payload);

  // write len + padded payload
  uint8_t page[1 + LOT_MAX_LEN];
  page[0] = len;
  for (uint8_t i=0; i<LOT_MAX_LEN; i++) page[i+1] = (i < len) ? uint8_t(payload[i]) : 0;

  if (!at24_write(hal::I2CAddr::AT24, LOT_OFF, page, sizeof(page))) return false;

  // write CRC
  uint8_t crc = crc8_xor(page, 1 + len);
  if (!at24_write(hal::I2CAddr::AT24, LOT_CRC_OFF, &crc, 1)) return false;

  // verify
  uint8_t len_rb=0, data_rb[LOT_MAX_LEN]={0}, crc_rb=0;
  if (!at24_read(hal::I2CAddr::AT24, LOT_OFF, &len_rb, 1)) return false;
  if (len_rb > LOT_MAX_LEN) return false;
  if (!at24_read(hal::I2CAddr::AT24, LOT_DATA_OFF, data_rb, LOT_MAX_LEN)) return false;
  if (!at24_read(hal::I2CAddr::AT24, LOT_CRC_OFF, &crc_rb, 1)) return false;

  uint8_t crc_chk = crc8_xor(&len_rb, 1);
  for (uint8_t i=0; i<len_rb; i++) crc_chk ^= data_rb[i];
  return (crc_rb == crc_chk);
}

static bool read_lot(char* out, uint8_t out_size) {
  if (out_size == 0) return false;
  uint8_t len=0, crc=0, data[LOT_MAX_LEN]={0};
  if (!at24_read(hal::I2CAddr::AT24, LOT_OFF, &len, 1)) return false;
  if (len > LOT_MAX_LEN) return false;
  if (!at24_read(hal::I2CAddr::AT24, LOT_DATA_OFF, data, LOT_MAX_LEN)) return false;
  if (!at24_read(hal::I2CAddr::AT24, LOT_CRC_OFF, &crc, 1)) return false;

  uint8_t crc_chk = crc8_xor(&len, 1);
  for (uint8_t i=0; i<len; i++) crc_chk ^= data[i];
  if (crc_chk != crc) return false;

  uint8_t n = min<uint8_t>(len, out_size-1);
  memcpy(out, data, n);
  out[n] = '\0';
  return true;
}

/* ================== CLI ================== */
static uint8_t currentCh = 0xFF;
static bool serialWasConnected = false;
static bool muxDetectedAtBoot = false;

static bool serial_connected() {
#if defined(ARDUINO_USB_CDC_ON_BOOT) && (ARDUINO_USB_CDC_ON_BOOT == 1)
  return static_cast<bool>(Serial);
#else
  return true;
#endif
}

static void print_prompt() {
  Serial.print("> ");
}

static void print_mux_status() {
  if (muxDetectedAtBoot) {
    Serial.printf("TCA9548A connected at 0x%02X\n", g_tcaAddr);
  } else {
    Serial.println("ERROR: TCA9548A not found on 0x70..0x77. Check power, wiring, and reset.");
  }
}

static void print_help() {
  Serial.println("Commands:");
  Serial.println("  CH <n>    - select mux channel n (0..7)");
  Serial.println("  MUX       - probe and report TCA9548A address on the main I2C bus");
  Serial.println("  BUSSCAN   - scan the main I2C bus before the mux");
  Serial.println("  SCAN      - scan I2C on current channel");
  Serial.println("  W <id>    - write ID at 0x00..0x02 and verify");
  Serial.println("  R         - read back ID + CRC");
  Serial.println("  LOT <txt> - store Lot# (A-Z 0-9 -), max 15 chars");
  Serial.println("  LR        - read back Lot#");
  Serial.println("  OFF       - deselect mux (all channels off)");
  Serial.println("  HELP      - show this help");
}

static void print_banner() {
  Serial.println("\nTGS2611 Module Writer (ID + Lot, mux-aware, no digipot programming)");
  print_mux_status();
  print_help();
  print_prompt();
}

static void poll_serial_attach() {
  const bool connected = serial_connected();
  if (connected && !serialWasConnected) {
    print_banner();
  }
  serialWasConnected = connected;
}

void setup(){
  pinMode(LDO_SENS_EN, OUTPUT);
  digitalWrite(LDO_SENS_EN, HIGH);     // power sensors if needed
  delay(10);

  hal::Mux::init(TCA_RESET);
  Serial.begin(115200);
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);
  delay(100);

  const uint32_t waitStart = millis();
  while (!serial_connected() && (millis() - waitStart) < 2500) {
    delay(10);
  }

  g_tcaAddr = TCA_ADDR;
  muxDetectedAtBoot = tcaPresent();
  poll_serial_attach();
}

void loop(){
  poll_serial_attach();

  static String line;
  while (Serial.available()){
    char c = Serial.read();
    if (c=='\r' || c=='\n') {
      line.trim();
      if (line.length()){
        String cmd = line; cmd.toUpperCase();

        if (cmd.startsWith("CH ")) {
          int n = cmd.substring(3).toInt();
          if (n<0 || n>7) Serial.println("Bad channel (0..7)");
          else if (!tcaPresent()) Serial.println("Mux not detected on main I2C bus (searched 0x70..0x77)");
          else if (tcaSelect((uint8_t)n)) { currentCh = (uint8_t)n; Serial.printf("Channel %d selected\n", n); }
          else Serial.println("Mux select FAILED");

        } else if (cmd == "MUX") {
          if (tcaPresent()) Serial.printf("TCA9548A detected at 0x%02X\n", g_tcaAddr);
          else Serial.println("TCA9548A NOT detected on 0x70..0x77");

        } else if (cmd == "BUSSCAN") {
          scan_main_bus();

        } else if (cmd == "OFF") {
          tcaOff(); currentCh = 0xFF; Serial.println("Mux off");

        } else if (cmd == "SCAN") {
          if (currentCh>7) Serial.println("Select CH first"); else i2c_scan_channel(currentCh);

        } else if (cmd == "R") {
          if (currentCh>7) { Serial.println("Select CH first"); }
          else {
            uint16_t id=0; bool ok=false;
            if (read_id(id, ok)) Serial.printf("ID=%u, CRC=%s\n", id, ok?"OK":"BAD");
            else Serial.println("EEPROM read FAILED");
          }

        } else if (cmd.startsWith("W ")) {
          if (currentCh>7) { Serial.println("Select CH first"); }
          else {
            uint16_t id = cmd.substring(2).toInt();
            Serial.printf("CH=%u  Writing ID %u ...\n", currentCh, id);
            if (!write_id(id)) Serial.println("EEPROM write/verify FAILED");
            else Serial.println("DONE.");
          }

        } else if (cmd.startsWith("LOT ")) {
          if (currentCh>7) Serial.println("Select CH first");
          else {
            String s = line.substring(4);
            if (write_lot(s.c_str())) Serial.println("Lot stored.");
            else Serial.println("Lot write FAILED");
          }

        } else if (cmd == "LR") {
          if (currentCh>7) Serial.println("Select CH first");
          else {
            char lot[LOT_MAX_LEN+1];
            if (read_lot(lot, sizeof(lot))) Serial.printf("Lot=%s\n", lot);
            else Serial.println("Lot read FAILED");
          }

        } else if (cmd == "HELP") {
          print_help();

        } else {
          Serial.println("Unknown. Type HELP.");
        }
      }
      line = "";
      print_prompt();
    } else {
      line += c;
    }
  }
}
