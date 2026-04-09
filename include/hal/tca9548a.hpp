#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <cstdint>

#include "i2c_addresses.hpp"
#include "mux_map.hpp"

namespace hal::Mux {

  struct Tca9548State { // Cache last selected channel
    int current = -1;  // -1 means unknown (equivalent to 0xFF)
  };

  inline bool select_channel(TwoWire& wire, uint8_t tca_addr, int ch, Tca9548State& state) {
    if (ch < 0 || ch > 7) return false; // invalid channel
    if (state.current == ch) return true; // already selected

    const uint8_t mask = static_cast<uint8_t>(1u << ch); // single bit mask (select only one channel)
    wire.beginTransmission(tca_addr);
    wire.write(mask);
    bool ok = (wire.endTransmission() == 0);
    if (ok) state.current = ch;
    return ok;
  }

  // Overload converting enum class Ch to int
  inline bool select_channel(TwoWire& wire, uint8_t addr, Ch ch, Tca9548State& state) {
    return select_channel(wire, addr, static_cast<int>(ch), state);
  }

  // Enum overload using default TCA9548A address
  inline bool select_channel(TwoWire& wire, Ch ch, Tca9548State& state) {
    return select_channel(wire, hal::I2CAddr::TCA9548A, ch, state);
  }

  // Int overload using default TCA9548A address
  inline bool select_channel(TwoWire& wire, int ch, Tca9548State& state) {
    return select_channel(wire, hal::I2CAddr::TCA9548A, ch, state);
  }

  // Initialize TCA9548A with reset pulse
  inline void init(uint8_t reset_pin) {
    pinMode(reset_pin, OUTPUT);
    digitalWrite(reset_pin, LOW);       // hold in reset
    delay(10);
    digitalWrite(reset_pin, HIGH);      // release reset
    delay(50);                          // stabilization
  }

  // Verify TCA9548A is present on I2C bus
  inline bool probe(TwoWire& wire, uint8_t tca_addr = hal::I2CAddr::TCA9548A) {
    wire.beginTransmission(tca_addr);
    return wire.endTransmission() == 0;
  }

}
