#include "app/boot_read.hpp"
#include "../common/sensors/tgs_eeprom.hpp"
#include "../common/calib/tgs_calibration.hpp"
#include "../common/drivers/isl22317.hpp"

using hal::Mux::Ch;

namespace {

template <typename It>
void scan_group(const char* label,
                It first, It last,
                hal::Mux::Tca9548State& muxState,
                TwoWire& wire,
                uint8_t mux_addr,
                bool include_series_1k)
{
  for (It it = first; it != last; ++it) {
    Ch ch = *it;
    if (!hal::Mux::select_channel(wire, mux_addr, ch, muxState)) {
      Serial.printf("%s  CH=%d  MUX SEL: FAIL\n", label, static_cast<int>(ch));
      continue;
    }

    uint16_t id = 0; bool crc_ok = false;
    bool id_ok = tgs_read_sensor_id_on_selected(id, crc_ok);

    uint8_t wcode = 0; bool w_ok = isl22317_read_wiper_on_selected(wcode);

    if (!id_ok && !w_ok) {
      Serial.printf("%s  CH=%d  EEPROM: FAIL  POT: FAIL\n",
                    label, static_cast<int>(ch));
      continue;
    }

    const char* crc_str = crc_ok ? "CRC:OK" : "CRC:BAD";
    float pot_k = kohms_for_wiper_code(wcode, 10.0f);
    float total_k = include_series_1k ? (pot_k + 1.0f) : pot_k;

    if (id_ok && w_ok) {
      Serial.printf("%s  CH=%d  ID=%u  %s  WIPER=%u  POT=%.3f kΩ%s\n",
                    label, static_cast<int>(ch), id, crc_str, wcode,
                    include_series_1k ? total_k : pot_k,
                    include_series_1k ? " (incl. +1k)" : "");
    } else if (id_ok) {
      Serial.printf("%s  CH=%d  ID=%u  %s  POT:FAIL\n",
                    label, static_cast<int>(ch), id, crc_str);
    } else { // only pot ok
      Serial.printf("%s  CH=%d  ID:FAIL  %s  WIPER=%u  POT=%.3f kΩ%s\n",
                    label, static_cast<int>(ch), crc_str, wcode,
                    include_series_1k ? total_k : pot_k,
                    include_series_1k ? " (incl. +1k)" : "");
    }
  }
}

} // namespace

namespace app {

void print_tgs_boot_status(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire,
                           uint8_t mux_addr,
                           bool include_series_1k)
{
  Serial.println("=== TGS status at boot ===");

  // TGS2611 channels (from hal::Mux::TGS2611)
scan_group("TGS2611",
            hal::Mux::TGS2611.begin(), hal::Mux::TGS2611.end(),
            muxState, wire, mux_addr, include_series_1k);

  // TGS2616 channels (if any configured)
scan_group("TGS2616",
           hal::Mux::TGS2616.begin(), hal::Mux::TGS2616.end(),
           muxState, wire, mux_addr, include_series_1k);

  Serial.println("==========================");
}

} // namespace app
