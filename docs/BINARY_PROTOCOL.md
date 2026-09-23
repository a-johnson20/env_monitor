# Binary Serial Protocol Implementation

## Overview

The serial communication protocol has been completely refactored from a human-readable text-based interface to an efficient binary protocol. This dramatically reduces bandwidth requirements while maintaining full functionality.

## Key Improvements

### Bandwidth Reduction
- **Before**: Text-based commands like "2" for WiFi menu, "1" for scan, etc., plus verbose responses
- **After**: Single-byte commands, compact binary responses
- **Savings**: ~70-90% reduction in serial overhead

### Protocol Structure

#### Commands (Client → Device)
All commands are single-byte opcodes:

```
0x10 - MAIN_MENU       (No payload)
0x11 - LIVE_START      (No payload)
0x12 - LIVE_STOP       (No payload)
0x13 - WIFI_MENU       (No payload)
0x14 - WIFI_SCAN       (No payload)
0x15 - WIFI_CONNECT    (SSID length + SSID + Password length + Password)
0x16 - WIFI_CONNECT_EAP (SSID + Username + Password, each with length prefix)
0x17 - WIFI_SAVED_LIST (No payload)
0x18 - WIFI_DISCONNECT (No payload)
0x19 - WIFI_FORGET     (Network index as single byte)
0x1A - WIFI_STATUS     (No payload)
0x1B - LOG_MENU        (No payload)
0x1C - LOG_LIST        (No payload)
0x1D - LOG_GET         (File index as single byte)
0x1E - RTC_TIME        (No payload)
0x1F - BACK            (No payload)
0x21 - PUMP_SET        (Speed percent as single byte; stored as the running speed and
                        applied only while the pump enable switch is latched)
0x22 - PUMP_GET        (No payload; replies PUMP_STATUS)
0x26 - EEPROM_WRITE    (Channel + TGS ID (u16 BE) + length-prefixed lot + length-prefixed board ID)
0x27 - EEPROM_READ     (Channel as single byte; replies EEPROM_IDS CSV "id,crc,lot,board,calib")
0x28 - EEPROM_CLEAR    (Channel as single byte; erases the whole EEPROM map — coefficients,
                        calibration time, ID/lot/board — back to blank 0xFF)
0x29 - FW_VERSION      (No payload; replies FW_VERSION string, e.g. "1.0.0")
0x2A - CALIB_TIMESTAMP (Channel + Unix time as u32 LE; stores the calibration
                        date/time in the sensor EEPROM)
0x2B - BOARD_ID_WRITE  (Length-prefixed board ID string; stored in ESP32 NVS)
0x2C - BOARD_ID_READ   (No payload; replies BOARD_ID, length-prefixed; empty = not set)
0x2D - BOARD_ID_CLEAR  (No payload; erases the mainboard ID from ESP32 NVS)
```

#### Response Types (Device → Client)

```
0x00 - OK              (No payload, indicates success)
0x01 - ERROR           (Error code as single byte)
0x02 - STATUS          (Length-prefixed message string)

0x10 - WIFI_LIST       (Number of networks, then for each: rssi + security + ssid_len + ssid)
0x11 - WIFI_INFO       (Connected flag + rssi + ssid + ip + gateway + dns, all length-prefixed)
0x12 - WIFI_SAVED      (Number of networks, then each network)
0x13 - LOG_LIST        (Number of files, then for each: size (4 bytes LE) + path_len + path)
0x14 - LOG_BEGIN       (File size + path with length prefix)
0x15 - LOG_DATA        (Chunk length + data)
0x16 - LOG_END         (File size + path with length prefix)

0x20 - LIVE_DATA       (CSV data - raw sensor readings)
0x21 - RTC_RESPONSE    (Time string with length prefix)
0x22 - PUMP_STATUS     (Pump speed percent + enable-switch latched flag, 2 bytes)
0x23 - LORA_INFO       (DevEUI,AppEUI,AppKey CSV string with length prefix)
0x24 - EEPROM_IDS      (TGS identity CSV "id,crc,lot,board,calib" with length prefix; calib is
                        the decimal Unix time of the last calibration, empty if never
                        calibrated, "ERR" if its CRC is bad)
0x25 - FW_VERSION      (Firmware version string "MAJOR.MINOR.PATCH" with length prefix)
0x26 - BOARD_ID        (Mainboard ID, length-prefixed; empty string = never set)
```

#### Error Codes

```
0x01 - INVALID_CMD     (Unknown command)
0x02 - SD_ERROR        (SD card error)
0x03 - WIFI_ERROR      (WiFi operation failed)
0x04 - TIMEOUT         (Operation timed out)
0x05 - INVALID_INDEX   (Index out of range)
0x06 - NO_NETWORKS     (No networks found)
0x07 - NOT_CONNECTED   (WiFi not connected)
0x08 - FILE_NOT_FOUND  (Log file not found)
```

### Length-Prefixed Strings

All strings in the protocol use a simple length-prefix format:
- 1 byte: length (0-255)
- N bytes: string data in UTF-8

Example: To send "MySSID":
```
Byte 0: 0x06      (length = 6)
Byte 1-6: M y S S I D
```

### Multi-Value Payloads

Some commands require multiple values. They're sent sequentially:

**WiFi Connect (PSK):**
1. Command byte (0x15)
2. SSID (length-prefixed string)
3. Password (length-prefixed string)

**WiFi Connect (EAP):**
1. Command byte (0x16)
2. SSID (length-prefixed string)
3. Username (length-prefixed string)
4. Password (length-prefixed string)

## Pump Control and Enable Switch

The pump PWM output is gated by the rig's latching enable switch (`PUMP_SWITCH_PIN`,
GPIO39: idle high, latched = pulled low). The firmware only drives the pump while
the switch is latched, so a speed can be set at any time but the pump runs only
with the switch closed — opening the switch stops the pump without forgetting
the speed, and latching it again resumes that speed.

`0x21` (PUMP_SET) takes one byte, the speed in percent. It stores the value in
ESP32 NVS (namespace `pump`, key `pct`) and applies it through the switch gate.

`0x22` (PUMP_GET) replies with `PUMP_STATUS` (also `0x22`) as `[type][len=2]
[percent][latched]`, where `latched` is 1 while the enable switch is closed.
Firmware up to 1.3.x replied with `len=1` and the percent only; a client must
consume the whole frame it is told about (the GUI treats the missing latched
byte as "unknown" and keeps working with those revisions).

The firmware also *pushes* a `PUMP_STATUS` frame unsolicited, the moment the
debounced switch state changes, using the exact same `[type][len=2][percent]
[latched]` encoding as the `PUMP_GET` reply (deferred until the serial menu
isn't mid-response, so it can never land inside another command's reply).
Clients that don't expect unsolicited frames can simply ignore/discard it —
nothing about the request/response protocol changes — but a client that reads
the port while idle no longer has to wait for its next poll to learn the
switch moved.

On the GUI side the Pump Control panel is a speed slider alone — there are no
Set/Off buttons, so each slider change is sent automatically (debounced by
`PUMP_SEND_DEBOUNCE_MS`). A background listener reads the port whenever no
command is in flight and applies the pushed `PUMP_STATUS` frame the instant it
arrives; a slower `PUMP_POLL_MS` poll is kept only as a safety net in case a
push is ever missed. Neither the push nor the poll ever moves the slider
handle, so toggling the switch cannot change the displayed speed — the switch
line shows the slider's own value instead of echoing a still-in-flight device
reading, and it's greyed out during live streaming since the port is owned by
the live-data reader then. The device's stored speed is read into the slider
only once, when the GUI connects.


## TGS Sensor EEPROM Map (calibration timestamp)

The GUI stamps every successful TGS2611 calibration with the current time and
stores it in the sensor's own EEPROM (AT24C02-compatible, on the same mux
channel), so the date travels with the sensor, not with the board:

| Addresses  | Contents                                                        |
|------------|-----------------------------------------------------------------|
| 0x00..0x02 | TGS sensor ID — [u16 BE id][XOR CRC]                            |
| 0x03..0x07 | R2ppm — [f32 LE][XOR CRC]                                       |
| 0x08..0x0C | Shah alpha — [f32 LE][XOR CRC]                                  |
| 0x0D..0x11 | Calibration unix time (UTC) — [u32 LE epoch][XOR CRC]           |
| 0x20..0x30 | Lot # — [len u8][15 ASCII zero-padded][XOR CRC of len+payload]  |
| 0x40..0x60 | Board ID — [len u8][31 ASCII zero-padded][XOR CRC at 0x60]      |

Write path: after `0x23` (CALIB_MULTIPOINT) returns OK, the GUI automatically
sends `0x2A` (CALIB_TIMESTAMP) with `[ch: u8][unix: u32 LE]`. Older firmware
that does not know `0x2A` replies `ERROR/1`, which the GUI reports as
"(calib time not stored)" without failing the calibration itself. `0x28`
(EEPROM_CLEAR) performs a full wipe: it erases every block above (including
the R2ppm/alpha coefficients and the calibration time) back to blank 0xFF,
so the sensor reads as uncalibrated afterwards.

## CSV Log File Metadata

Every new `/logs/*.csv` file starts with a `#META` line above the heading,
snapshotted once at file creation (one file per RTC day):

```
#META,mainboard_id=MB-01,tgs2611_1_id=10,tgs2611_1_board=BRD-01,tgs2611_1_lot=462VV,tgs2611_1_cal=2026-09-22 15:04
timestamp,scd4x_1_co2,...
```

Fields: the mainboard ID (ESP32 NVS) and, per TGS2611 channel, the sensor ID
(`?` suffix = CRC mismatch), board ID, lot number and calibration date (UTC,
from the unix time stored via `0x2A`). Missing values print as `(none)`,
`(never)`, or `NA` (sensor EEPROM unreadable). The `#` prefix lets CSV tools
treat the line as a comment; `gui/download_csv.py` transfers it verbatim, and
the GUI logs viewer skips it when parsing (so the heading stays row 1) while
preserving the line in Excel-safe exports.

In the GUI the key/value pairs are shown in the muted "Meta:" line directly
under the preview title (`Preview: /logs/....csv (N bytes)`) when a file is
selected — the line is packed above the preview/graph pane so it cannot be
clipped, and is hidden entirely when it has nothing to say. Files with no
comment row at all display `Meta: (no #META line in this file)`, which
distinguishes pre-1.3.0 log files from a GUI display problem.

## SD Card Error Handling (`ERROR` / `SD_ERROR` = 0x02)

`0x1C` (LOG_LIST) and `0x1D` (LOG_GET) reply `ERROR` with code `0x02` when the
card's `/logs` directory cannot be opened. Two safeguards keep this from
surfacing spuriously during a refresh:

- **Firmware, hot-plug probe** (`sd_logger::poll_hotplug`): the card is only
  declared removed after 4 consecutive failed presence probes (~2 s at the
  500 ms poll rate), so a single transient `open()` miss no longer tears down
  the SDMMC driver.
- **Firmware, file list** (`refresh_log_files`): mounts on demand if the logger
  is unmounted (e.g. mid-back-off) and retries once with a fresh mount, so a
  card that has been re-inserted lists immediately.
- **GUI** (`SerialMenuClient.list_logs`): on `SD_ERROR` it re-issues `0x1C`
  once before reporting failure, and error codes are rendered as readable text
  (`Device error 2 (SD card error ...)`) instead of a bare number.

## Files Modified

### Firmware Side

1. **include/ui/serial_protocol.hpp** (NEW)
   - Protocol definitions (command/response codes)
   - Helper function declarations
   - Payload structure definitions

2. **src/ui/serial_protocol.cpp** (NEW)
   - Helper function implementations
   - Binary data writing functions
   - String encoding

3. **src/ui/serial_menu.cpp** (MODIFIED)
   - Completely refactored for binary protocol
   - Removed text-based menu system
   - Replaced with binary command parser
   - All responses now binary-encoded

4. **src/app/main.cpp** (MODIFIED)
   - Added `get_rtc_time_string()` function
   - Kept existing `print_rtc_time()` for debugging

### GUI Side

1. **gui/env_monitor_gui.py** (COMPLETELY REPLACED)
   - New binary protocol client implementation
   - Binary command sending methods
   - Binary response parsing
   - All operations updated for binary protocol
   - Maintained same UI/UX

2. **gui/env_monitor_gui.py.bak**
   - Backup of original text-based GUI

## Usage Notes

### For End Users

- The GUI has been completely replaced with a new binary protocol version
- All functionality remains the same:
  - Live data streaming
  - Log file download
  - WiFi management
  - RTC time display
- Performance should be **faster** due to reduced overhead
- Works with firmware built from the updated code

### For Developers

If you need to add new commands:

1. Add command opcode to `serial_protocol.hpp` (ProtoCmd class)
2. Add response type if needed (ProtoResp class)
3. Implement in `serial_menu.cpp` poll() function
4. Add corresponding GUI method
5. Update this documentation

### Backward Compatibility

**BREAKING CHANGE**: The new binary protocol is NOT compatible with the old text-based protocol. You must use:
- **New firmware** (with binary protocol) OR
- **Old firmware** (with text protocol)

Both can use the corresponding GUI version.

## Versioning (GUI ↔ Firmware)

Both sides carry an independent `MAJOR.MINOR.PATCH` version so a mismatch is
visible before it turns into mysterious protocol errors.

| Side | Where the version lives | Reported by |
|------|-------------------------|-------------|
| GUI | `GUI_VERSION` near the top of `gui/env_monitor_gui.py` (just after the imports) | Settings > About (read-only field) |
| Firmware | `FIRMWARE_VERSION` near the top of `src/app/main.cpp` (just after the includes), persisted to ESP32 NVS (namespace `fw`, key `version`) | `FW_VERSION` command (0x29) |

Details:

* Both constants are deliberately declared **high up in their file** so a version
  bump is the first thing you see when you open it — no searching required.
  Bump them manually as part of the change (see the SemVer rule below).

* The firmware version is stored on the ESP32 (NVS) so it is queryable without
  a rebuild, and it is **re-seeded from the `FIRMWARE_VERSION` constant on every
  boot**. NVS therefore always mirrors the flashed binary: bump the constant,
  re-flash, and the device reports the new version. Editing the NVS entry by
  hand cannot spoof a version the running binary was not built with, because the
  next boot overwrites it.
* The GUI fetches the firmware version immediately on connect (and on
  "Refresh" in Settings > About) and shows a compatibility verdict:
  **green** = exact protocol match, **amber** = compatible but the minor version
  differs, **red** = incompatible, **grey** = version unreadable. The verdict is
  plain prose with no tick/cross glyphs (readable with any font/encoding), and
  the colour is the only extra indicator.
* **Compatibility rule (SemVer)** — mismatched versions are graded rather than
  simply rejected, because minor releases are additive. The verdict text is
  always one of exactly four forms, where **`protocol N` is the MAJOR number**
  (the serial-protocol revision); the full version strings stay visible in the
  About fields:

  | Verdict text | Condition | Level / colour | Meaning |
  |--------------|-----------|----------------|---------|
  | `Compatible (protocol 1)` | same `MAJOR.MINOR` | green | Both sides are the same protocol revision |
  | `Compatible (protocol 1, minor version differs)` | same `MAJOR`, different `MINOR` | amber | Additive change only, so the pair still interoperates; the newer side may offer features the older one cannot call |
  | `Incompatible (GUI protocol 2, firmware protocol 1)` | different `MAJOR` | red | Breaking reorganisation — re-pair GUI and firmware |
  | `Compatibility unknown (firmware protocol unknown)` | firmware version unreadable | grey | Device did not answer `0x29` (pre-1.0.0 firmware) or sent unusable text |

  `PATCH` differences are always interchangeable (identical wire format) and are
  not reported at all, so they still read as `Compatible (protocol 1)`. The
  unknown case is deliberately minimal: the raw device reply stays visible in the
  About "Firmware version" field, and the reason for a failed read (e.g. too old
  to support `0x29`) is reported on the main status line rather than in the
  verdict.
* Bump `MINOR` for additive protocol changes (new command/response, extra field),
  `PATCH` for fixes that keep the wire format identical, `MAJOR` for breaking
  reorganisations. Bump **both** sides in the same commit when the protocol
  changes — that is what makes the About check meaningful.
* Firmware older than the shipped release may not implement a given command at
  all (e.g. `0x29`): the GUI surfaces that as an error on the action rather than
  crashing, which is why an amber/unknown verdict is informational only.

## Testing the Protocol

### Manual Testing with Python

```python
import serial
import time

ser = serial.Serial('COM4', 115200, timeout=1)
time.sleep(0.2)

# Send WIFI_SCAN command
ser.write(bytes([0x14]))
ser.flush()

# Read response type
resp_type = ser.read(1)[0]
print(f"Response type: 0x{resp_type:02x}")

# Read number of networks
num = ser.read(1)[0]
print(f"Found {num} networks")

ser.close()
```

### Live Monitoring

The live data stream still outputs CSV format for compatibility with existing data processing. Only the menu navigation is binary-encoded.

## Performance Improvements

### Example: WiFi Scan + Connect

**Old Protocol (Text)**:
```
>>> 2\n          (4 bytes - WiFi menu)
[menu output]    (200+ bytes)
>>> 1\n          (4 bytes - Scan)
[network list]   (500+ bytes for 10 networks)
>>> 1\n          (2 bytes - Select network)
>>> password\n   (20+ bytes)
[responses]      (100+ bytes)
TOTAL: ~850 bytes
```

**New Protocol (Binary)**:
```
0x14             (1 byte - WIFI_SCAN)
0x10 + count     (2 bytes - Response header + count)
[networks]       (~40 bytes for 10 networks, ~4 bytes each)
0x15 + len+...   (1 + 1 + len + 1 + len bytes - WIFI_CONNECT)
0x00             (1 byte - OK response)
TOTAL: ~50-70 bytes
```

**Improvement: ~85-90% reduction in bytes transferred**

## Notes

- Serial line endings (\r, \n) are NOT used in binary protocol
- All numeric values (except lengths) are encoded as bytes or multi-byte integers
- The implementation maintains thread safety with locks
- Live data continues to use line-based CSV format for compatibility

## Future Enhancements

Possible improvements for future versions:
- Delta encoding for sensor data in live stream
- Huffman compression for WiFi networks list
- Packet checksums for error detection
- Configurable baud rate reporting
