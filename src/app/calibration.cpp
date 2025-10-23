#include "app/calibration.hpp"
#include "common/calib/tgs_calibration.hpp"
#include "common/tgs_lookup_tables.hpp"

using hal::Mux::Ch;

namespace app {

void calibrate_all_tgs2611(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire,
                           uint8_t mux_addr,
                           uint8_t default_wiper) {
  for (Ch ch : hal::Mux::TGS2611) {
    if (hal::Mux::select_channel(wire, mux_addr, ch, muxState)) {
      // Your existing function:
      calibrate_tgs_on_selected(TGS2611_CAL, N_TGS2611_CAL, "TGS2611", default_wiper);
    }
  }
}

void calibrate_all_tgs2616(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire,
                           uint8_t mux_addr,
                           uint8_t default_wiper) {
  for (Ch ch : hal::Mux::TGS2616) {
    if (hal::Mux::select_channel(wire, mux_addr, ch, muxState)) {
      calibrate_tgs_on_selected(TGS2616_CAL, N_TGS2616_CAL, "TGS2616", default_wiper);
    }
  }
}

}
