#pragma once
#include <Arduino.h>
#include <Wire.h>

#include "hal/mux_map.hpp"
#include "hal/tca9548a.hpp"
#include "hal/i2c_addresses.hpp"

namespace app {
// Print TGS status at boot for all mapped channels (ID, CRC, wiper code, kΩ).
void print_tgs_boot_status(hal::Mux::Tca9548State& muxState,
                           TwoWire& wire = ::Wire,
                           uint8_t mux_addr = hal::I2CAddr::TCA9548A,
                           bool include_series_1k = false);
}
