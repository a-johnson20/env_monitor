#pragma once

#include <Arduino.h>

namespace sensors {

struct PlatinumN2oReading {
  bool valid = false;
  float ppm = NAN;
  unsigned long last_ok_ms = 0;
};

// Driver for the Dynament/PST Platinum Series N2O sensor on a UART port.
//
// Protocol: binary DLE-ETX framed, 38400 8N1.
// Request  ("Read Live Data Simple"): 7 bytes
//   [DLE=0x10][CMD=0x13][data: 0x06 0x00][DLE=0x10][ETX=0x1F][CHK_HI=0x00][CHK_LO=0x58]
//   16-bit checksum = sum of all bytes from first DLE through ETX inclusive.
// Response: variable-length DLE-ETX packet
//   [DLE=0x10][CMD=0x1A][data...][DLE=0x10][ETX=0x1F][CHK_HI][CHK_LO]
//   Data byte 0   : sub-header / length
//   Data byte 1   : status
//   Data bytes 2-5: gas concentration, IEEE-754 float LE
//   DLE stuffing  : 0x10 0x10 in data stream = literal 0x10 data byte.
class PlatinumN2oUart {
 public:
  bool begin(HardwareSerial& serial,
             int8_t rx_pin,
             int8_t tx_pin,
             uint32_t baud = 38400,
             uint32_t config = SERIAL_8N1);

  // Call every loop iteration. Sends the request every kPollIntervalMs and
  // drains available RX bytes. Returns true when a new valid reading is ready.
  bool poll(PlatinumN2oReading& out);

 private:
  enum class State : uint8_t {
    WAIT_HDR1,    // waiting for DLE (0x10)
    WAIT_CMD,     // waiting for CMD byte
    READ_DATA,    // accumulating data bytes until DLE+ETX
    READ_CHK_HI,  // reading high byte of 16-bit checksum
    READ_CHK_LO,  // reading low byte; verify checksum
  };

  void reset_frame_();
  bool verify_checksum_(uint8_t chk_hi, uint8_t chk_lo) const;

  HardwareSerial* serial_ = nullptr;

  // Frame assembly
  State state_ = State::WAIT_HDR1;
  uint8_t chk_hi_ = 0;             // high byte of received checksum
  static constexpr size_t kMaxPayload = 32;
  uint8_t payload_[kMaxPayload]{};
  uint8_t payload_len_ = 0;
  bool dle_pending_ = false;        // last byte was 0x10 inside data section
  uint16_t checksum_accum_ = 0;    // running 16-bit sum of all framing bytes

  // Periodic request timing
  static constexpr unsigned long kPollIntervalMs = 5000;
  unsigned long last_poll_ms_ = 0;
};

} // namespace sensors
