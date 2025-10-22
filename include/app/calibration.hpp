#pragma once

#include <cstdint>
#include <Wire.h>

#include "hal/mux_map.hpp"        // hal::Mux::Ch, TGS2611/TGS2616 arrays
#include "hal/tca9548a.hpp"       // hal::Mux::Tca9548State, select_channel
#include "hal/i2c_addresses.hpp"  // hal::I2CAddr::TCA9548A

namespace app {

// Runs calibration across all wired TGS2611 channels.
// default_wiper is whatever you use today (e.g., 80).
void calibrate_all_tgs2611(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire = ::Wire,
                           uint8_t mux_addr = hal::I2CAddr::TCA9548A,
                           uint8_t default_wiper = 80);

// Same for TGS2616 (e.g., default wiper 70)
void calibrate_all_tgs2616(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire = ::Wire,
                           uint8_t mux_addr = hal::I2CAddr::TCA9548A,
                           uint8_t default_wiper = 70);

}
