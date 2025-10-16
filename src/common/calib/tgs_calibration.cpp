// Project includes
#include "tgs_calibration.hpp"
#include "tgs_lookup_tables.hpp"
#include "../sensors/tgs_eeprom.hpp"
#include "../drivers/isl22317.hpp"

static bool find_wiper_for( const CalEntry* tab, size_t n, uint16_t id, uint8_t &wiper_out) {
  for (size_t i=0;i<n;i++) if (tab[i].id == id) { wiper_out = tab[i].wiper; return true; }
  return false;
}

bool calibrate_tgs_on_selected( const CalEntry* tab, size_t n, const char* label, uint8_t default_wiper) {
  uint16_t id = 0; bool crc_ok = false;
  if (!tgs_read_sensor_id_on_selected(id, crc_ok)) {
    Serial.printf("%s: EEPROM read FAILED\n", label);
    return false;
  }
  if (!crc_ok) {
    Serial.printf("%s: ID=%u but CRC BAD — using default wiper=%u\n", label, id, default_wiper);
    return isl22317_set_wiper_on_selected(default_wiper);
  }
  uint8_t w = 0;
  if (find_wiper_for(tab, n, id, w)) {
    bool ok = isl22317_set_wiper_on_selected(w);
    Serial.printf("%s: ID=%u → wiper=%u %s\n", label, id, w, ok?"(OK)":"(I2C FAIL)");
    return ok;
  } else {
    bool ok = isl22317_set_wiper_on_selected(default_wiper);
    Serial.printf("%s: ID=%u not in table — using default wiper=%u %s\n",
                  label, id, default_wiper, ok?"(OK)":"(I2C FAIL)");
    return ok;
  }
}
