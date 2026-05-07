#include "common/sensors/platinum_n2o_uart.hpp"

#include <string.h>

// Define N2O_DEBUG in platformio.ini build_flags to emit raw hex to Serial:
// -DN2O_DEBUG

namespace sensors {

// Request command: "Read Live Data Simple" (DLE-ETX framed, 16-bit checksum)
// [DLE=0x10][CMD=0x13][data: 0x06][DLE=0x10][ETX=0x1F][CHK_HI=0x00][CHK_LO=0x58]
// 16-bit checksum = sum(0x10+0x13+0x06+0x10+0x1F) = 0x0058
static const uint8_t kRequestCmd[] = {0x10, 0x13, 0x06, 0x10, 0x1F, 0x00, 0x58};

bool PlatinumN2oUart::begin(HardwareSerial& serial,
                            int8_t rx_pin,
                            int8_t tx_pin,
                            uint32_t baud,
                            uint32_t config) {
  serial_ = &serial;
  reset_frame_();
  last_poll_ms_ = 0;
  serial_->begin(baud, config, rx_pin, tx_pin);
  return true;
}

void PlatinumN2oUart::reset_frame_() {
  state_          = State::WAIT_HDR1;
  chk_hi_         = 0;
  payload_len_    = 0;
  dle_pending_    = false;
  checksum_accum_ = 0;
}

bool PlatinumN2oUart::verify_checksum_(uint8_t chk_hi, uint8_t chk_lo) const {
  return checksum_accum_ == (static_cast<uint16_t>(chk_hi) << 8 | chk_lo);
}

bool PlatinumN2oUart::poll(PlatinumN2oReading& out) {
  if (!serial_) return false;

  // Send request periodically
  const unsigned long now = millis();
  if (now - last_poll_ms_ >= kPollIntervalMs) {
    last_poll_ms_ = now;
    serial_->write(kRequestCmd, sizeof(kRequestCmd));
#ifdef N2O_DEBUG
    Serial.println("[N2O] REQUEST SENT");
#endif
  }

  bool updated = false;

#ifdef N2O_DEBUG
  static uint32_t dbg_byte_count = 0;
  bool dbg_any = false;
  const int avail = serial_->available();
  if (avail > 0) {
    Serial.print("[N2O] RX ");
    Serial.print(avail);
    Serial.print(" bytes: ");
  }
#endif

  while (serial_->available() > 0) {
    const int c = serial_->read();
    if (c < 0) break;
    const uint8_t b = static_cast<uint8_t>(c);

#ifdef N2O_DEBUG
    {
      char tmp[8];
      snprintf(tmp, sizeof(tmp), "%02X ", b);
      Serial.print(tmp);
      dbg_byte_count++;
      dbg_any = true;
    }
#endif

    switch (state_) {

      case State::WAIT_HDR1:
        if (b == 0x10) {
          checksum_accum_ = 0x10;
          state_ = State::WAIT_CMD;
        }
        break;

      case State::WAIT_CMD:
        // Accept response command byte (0x1A = "Read Live Data Simple" response).
        // Any 0x10 here re-starts the header search.
        if (b == 0x10) {
          checksum_accum_ = 0x10; // stay in WAIT_CMD looking for a non-DLE CMD
        } else if (b == 0x1A) {
          checksum_accum_ += b;
          payload_len_ = 0;
          dle_pending_ = false;
          state_ = State::READ_DATA;
        } else {
          // Ignore non-live-data frames so they cannot poison parser state.
          reset_frame_();
        }
        break;

      case State::READ_DATA:
        if (dle_pending_) {
          dle_pending_ = false;
          if (b == 0x10) {
            // DLE stuffing: literal 0x10 data byte
            checksum_accum_ += 0x10;
            if (payload_len_ < kMaxPayload) payload_[payload_len_++] = 0x10;
          } else if (b == 0x1F) {
            // DLE + ETX: end of data, add both to checksum
            checksum_accum_ += 0x10;
            checksum_accum_ += 0x1F;
            state_ = State::READ_CHK_HI;
          } else {
            // Re-sync heuristic: treat prior DLE as a new header and current byte as CMD.
            if (b == 0x1A) {
              checksum_accum_ = 0x10 + b;
              payload_len_ = 0;
              dle_pending_ = false;
              state_ = State::READ_DATA;
            } else {
              reset_frame_();
            }
          }
        } else if (b == 0x10) {
          dle_pending_ = true;
        } else {
          checksum_accum_ += b;
          if (payload_len_ < kMaxPayload) payload_[payload_len_++] = b;
        }
        break;

      case State::READ_CHK_HI:
        chk_hi_ = b;
        state_ = State::READ_CHK_LO;
        break;

      case State::READ_CHK_LO:
        // payload layout: [sub_hdr][status][float_b0..b3][...]
        if (verify_checksum_(chk_hi_, b) && payload_len_ >= 6) {
          // payload[1] = status, payload[2..5] = gas concentration (IEEE-754 LE)
          float gas_ppm;
          memcpy(&gas_ppm, &payload_[2], sizeof(float));
          if (isfinite(gas_ppm) && gas_ppm >= 0.0f) {
#ifdef N2O_DEBUG
            Serial.print("[N2O] PARSED ppm=");
            Serial.print(gas_ppm);
            Serial.print(" status=0x");
            Serial.println(payload_[1], HEX);
#endif
            out.ppm        = gas_ppm;
            out.valid      = true;
            out.last_ok_ms = millis();
            updated        = true;
          }
#ifdef N2O_DEBUG
          else {
            Serial.print("[N2O] REJECTED gas_ppm=");
            Serial.println(gas_ppm);
          }
#endif
        }
#ifdef N2O_DEBUG
        else if (!verify_checksum_(chk_hi_, b)) {
          Serial.print("[N2O] CHK FAIL got=");
          Serial.print(chk_hi_, HEX);
          Serial.print(b, HEX);
          Serial.print(" expected=");
          Serial.println(checksum_accum_, HEX);
        }
#endif
        reset_frame_();
        break;
    }
  }

#ifdef N2O_DEBUG
  if (dbg_any) {
    Serial.print("(total=");
    Serial.print(dbg_byte_count);
    Serial.println(")");
  }
  (void)avail;
#endif

  return updated;
}

} // namespace sensors
