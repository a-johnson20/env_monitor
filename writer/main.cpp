#include <Arduino.h>
#include <Wire.h>

// ====== BOARD/I2C/MUX PINS & ADDRS (adjust if needed) ======
#define I2C_SDA   5           // your sensor bus pins
#define I2C_SCL   6
#define TCA_ADDR  0x70        // TCA9548A
#define AT24_ADDR 0x50        // 0x50..0x57 depending on A2/A1/A0 on sensor
#define ISL_ADDR  0x28        // or 0x2A depending on A1 strap
#define LDO_SENS_EN 41        // if you need to power the sensor rail

// ====== MUX ======
bool tcaSelect(uint8_t ch) {
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(1 << ch);
  if (Wire.endTransmission() != 0) return false;
  delay(2);                   // small settle like in your main
  return true;
}
void tcaOff() {
  Wire.beginTransmission(TCA_ADDR);
  Wire.write(0);
  Wire.endTransmission();
}

// ====== I2C SCAN (on currently-selected channel) ======
void i2c_scan_channel() {
  for (uint8_t a = 1; a < 127; a++) {
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) {
      Serial.printf("I2C: 0x%02X\n", a);
    }
  }
}

// ====== AT24C02C (8-byte pages) ======
static const uint8_t AT24_PAGE = 8;
bool at24_write_bytes(uint8_t dev, uint8_t word, const uint8_t* d, uint8_t n){
  while(n){
    uint8_t pageOff = word % AT24_PAGE;
    uint8_t chunk   = min<uint8_t>(AT24_PAGE - pageOff, n);
    Wire.beginTransmission(dev); Wire.write(word); Wire.write(d, chunk);
    if (Wire.endTransmission()!=0) return false;
    // ACK polling (tWR ~5ms)
    uint32_t t0 = millis();
    while (millis()-t0 < 20) {
      Wire.beginTransmission(dev);
      if (Wire.endTransmission()==0) break;
      delay(1);
    }
    word += chunk; d += chunk; n -= chunk;
  }
  return true;
}
bool at24_read_bytes(uint8_t dev, uint8_t word, uint8_t* d, uint8_t n){
  Wire.beginTransmission(dev); Wire.write(word);
  if (Wire.endTransmission(false)!=0) return false;
  if (Wire.requestFrom(dev, n)!=n) return false;
  for(uint8_t i=0;i<n;i++) d[i]=Wire.read();
  return true;
}
uint8_t crc8(const uint8_t* p, uint8_t n){ uint8_t c=0; for(uint8_t i=0;i<n;i++) c^=p[i]; return c; }

// ====== ISL22317 ======
#define ISL_REG_WIPER_IVR 0x00
#define ISL_REG_MSR       0x01
#define ISL_REG_ACR       0x02
bool isl_w(uint8_t r, uint8_t v){
  Wire.beginTransmission(ISL_ADDR); Wire.write(r); Wire.write(v);
  return Wire.endTransmission()==0;
}
bool isl_r(uint8_t r, uint8_t &v){
  Wire.beginTransmission(ISL_ADDR); Wire.write(r);
  if (Wire.endTransmission(false)!=0) return false;
  if (Wire.requestFrom(ISL_ADDR,(uint8_t)1)!=1) return false;
  v = Wire.read(); return true;
}
bool isl_set_wiper_volatile(uint8_t w){   // no NV wear
  return isl_w(ISL_REG_ACR,0xC0) && isl_w(ISL_REG_WIPER_IVR,(uint8_t)(w & 0x7F));
}
bool isl_set_wiper_nv(uint8_t w){         // persists across power cycles
  if (!isl_w(ISL_REG_ACR,0x40)) return false;
  if (!isl_w(ISL_REG_WIPER_IVR,(uint8_t)(w & 0x7F))) return false;
  uint32_t t0 = millis();
  while (millis()-t0 < 30){
    uint8_t acr=0; if(!isl_r(ISL_REG_ACR,acr)) return false;
    if(((acr>>5)&1)==0) break; delay(1);
  }
  return true;
}

// ====== Your mapping: ID -> wiper (0..127). Replace with your rule/LUT. ======
uint8_t wiperFromID(uint16_t id){
  switch(id){ case 1001: return 22; case 1002: return 37; case 1003: return 64; case 1004: return 95; default: return 64; }
}

// ====== EEPROM writer helpers ======
bool write_id(uint16_t id){
  uint8_t buf[3] = { uint8_t(id>>8), uint8_t(id&0xFF), 0 };
  buf[2] = crc8(buf,2);
  if(!at24_write_bytes(AT24_ADDR, 0x00, buf, 3)) return false;
  uint8_t rb[3]={0};
  if(!at24_read_bytes(AT24_ADDR, 0x00, rb, 3)) return false;
  uint16_t rid = (uint16_t(rb[0])<<8) | rb[1];
  return (rid==id) && (crc8(rb,2)==rb[2]);
}

// ====== Simple CLI ======
uint8_t currentCh = 0xFF;

void help() {
  Serial.println("Commands:");
  Serial.println("  CH <n>      - select mux channel n (0..7)");
  Serial.println("  SCAN        - scan I2C on current channel");
  Serial.println("  W <id>      - write ID, verify, set VOLATILE wiper");
  Serial.println("  N <id>      - write ID, verify, store NV wiper");
  Serial.println("  R           - read back ID + CRC");
  Serial.println("  OFF         - deselect mux (all channels off)");
  Serial.println("  HELP        - show this help");
}

void setup(){
  pinMode(LDO_SENS_EN, OUTPUT);
  digitalWrite(LDO_SENS_EN, HIGH);    // power sensors if needed
  delay(10);

  Serial.begin(115200);
  Wire.begin(I2C_SDA, I2C_SCL);       // sensor bus
  delay(100);
  Serial.println("\nTGS2611 Writer (mux-aware)");
  help();
}

void loop(){
  static String line;
  while (Serial.available()){
    char c = Serial.read();
    if(c=='\r' || c=='\n'){
      line.trim();
      if(line.length()){
        String cmd = line; cmd.toUpperCase();
        if (cmd.startsWith("CH ")) {
          int n = cmd.substring(3).toInt();
          if (n<0 || n>7) { Serial.println("Bad channel (0..7)"); }
          else if (tcaSelect((uint8_t)n)) { currentCh = (uint8_t)n; Serial.printf("Channel %d selected\n", n); }
          else Serial.println("Mux select FAILED");
        } else if (cmd=="OFF") {
          tcaOff(); currentCh = 0xFF; Serial.println("Mux off");
        } else if (cmd=="SCAN") {
          if (currentCh>7) Serial.println("Select CH first");
          else i2c_scan_channel();
        } else if (cmd=="R") {
          if (currentCh>7) { Serial.println("Select CH first"); }
          else {
            uint8_t rb[3];
            if(at24_read_bytes(AT24_ADDR,0x00,rb,3)){
              uint16_t rid=(uint16_t(rb[0])<<8)|rb[1];
              Serial.printf("ID=%u, CRC=%02X (%s)\n", rid, rb[2], crc8(rb,2)==rb[2]?"OK":"BAD");
            } else Serial.println("EEPROM read FAILED");
          }
        } else if (cmd.startsWith("W ") || cmd.startsWith("N ")) {
          if (currentCh>7) { Serial.println("Select CH first"); }
          else {
            bool nv = (cmd[0]=='N');
            uint16_t id = cmd.substring(2).toInt();
            Serial.printf("CH=%u  Writing ID %u ...\n", currentCh, id);
            if(!write_id(id)) { Serial.println("EEPROM write/verify FAILED"); }
            else {
              uint8_t w = wiperFromID(id);
              bool ok = nv ? isl_set_wiper_nv(w) : isl_set_wiper_volatile(w);
              Serial.printf("%s wiper %s: %u\n", nv?"NV":"Volatile", ok?"OK":"FAILED", w);
            }
          }
        } else if (cmd=="HELP") {
          help();
        } else {
          Serial.println("Unknown. Type HELP.");
        }
      }
      line="";
    } else {
      line += c;
    }
  }
}
