"""Regression tests for the GUI's frame-aware serial protocol reader.

These run against a FakeSerial — no device or COM port needed:

    python test/gui_protocol_test.py

Covers the WiFi-connect failure modes reported in the field:
  * "Unexpected response: 91"  (a stray payload byte read as a response type)
  * "WiFi: Disconnected" flashing right after a successful connect
    (device busy frames / late replies misparsed as a disconnect report)
"""

import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))

import env_monitor_gui as gui  # noqa: E402  (needs the gui dir on sys.path)


class FakeSerial:
    """Minimal serial.Serial stand-in.

    `data`  — bytes available for reading immediately.
    `on_reset` — bytes that magically appear *after* reset_input_buffer() is
                 called. Simulates bytes that were already in flight over USB
                 when the GUI flushed the buffer (the real-world desync case).
    """

    def __init__(self, data: bytes = b"", on_reset: bytes = b""):
        self._buf = bytearray(data)
        self._on_reset = bytearray(on_reset)
        self.written = bytearray()
        self.is_open = True
        self.timeout = 0.1

    def read(self, n: int = 1) -> bytes:
        if not self._buf:
            return b""
        take = bytes(self._buf[:n])
        del self._buf[:n]
        return take

    def write(self, data) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._buf.clear()
        self._buf.extend(self._on_reset)

    def close(self) -> None:
        self.is_open = False


def make_client(data: bytes = b"", on_reset: bytes = b"") -> gui.SerialMenuClient:
    c = gui.SerialMenuClient()
    c.ser = FakeSerial(data, on_reset)
    return c


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------- test cases

def test_status_frame_before_ok_is_skipped():
    """Device emits an unsolicited STATUS frame, then OK → connect succeeds."""
    status = b"[RTC] Manual resync OK"
    data = bytes([0x02, len(status)]) + status + bytes([0x00])  # STATUS + OK
    c = make_client(on_reset=data)
    r = c.wifi_connect("MyNet", "pw", "PSK", timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(c.ser.written.startswith(bytes([0x15])), "WIFI_CONNECT cmd not sent")


def test_stray_payload_byte_does_not_break_connect():
    """A stray 0x5B ('[' = 91 — the reported popup byte) is skipped, OK still parsed."""
    data = bytes([0x5B]) + bytes([0x00])  # stray payload byte + OK
    c = make_client(on_reset=data)
    r = c.wifi_connect("MyNet", "pw", "PSK", timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")


def test_live_data_frame_is_skipped():
    """A LIVE_DATA frame (leftover stream) between command and OK is consumed."""
    line = b"2026-08-28 12:00:00,415,22.5"
    data = bytes([0xFE, len(line)]) + line + bytes([0x00])
    c = make_client(on_reset=data)
    r = c.wifi_connect("MyNet", "pw", "PSK", timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")


def test_late_rtc_reply_does_not_break_connect():
    """A late RTC_RESPONSE (from the periodic RTC poll) is consumed, OK parsed."""
    ts = b"2026-08-28 12:34:56"
    data = bytes([0x21, len(ts)]) + ts + bytes([0x00])
    c = make_client(on_reset=data)
    r = c.wifi_connect("MyNet", "pw", "PSK", timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")


def test_error_response_reports_status_text():
    """ERROR after a STATUS → failure with the device's status text surfaced."""
    status = b"WPA2 handshake failed"
    data = bytes([0x02, len(status)]) + status + bytes([0x01, 0x03])
    c = make_client(on_reset=data)
    r = c.wifi_connect("MyNet", "pw", "PSK", timeout_s=2.0)
    expect(r["success"] is False, f"expected failure, got {r}")
    expect("WPA2 handshake failed" in r["message"], f"status text missing: {r}")


def test_wifi_status_parses_connected_info():
    """WIFI_INFO for a connected device parses to ssid/rssi."""
    ssid = b"LabWiFi"
    frame = (bytes([0x11, 0x01, 0xC4])           # WIFI_INFO, connected, rssi=-60
             + bytes([len(ssid)]) + ssid
             + bytes([8]) + b"10.0.0.5"
             + bytes([8]) + b"10.0.0.1"
             + bytes([8]) + b"10.0.0.1")
    c = make_client(on_reset=frame)
    s = c.wifi_get_status(timeout_s=2.0)
    expect(s["ssid"] == "LabWiFi", f"ssid mismatch: {s}")
    expect(s["rssi"] == "-60", f"rssi mismatch: {s}")


def test_wifi_status_not_connected_is_explicit():
    """A genuine 'connected=0' report returns empty ssid (real disconnect)."""
    c = make_client(on_reset=bytes([0x11, 0x00]))
    s = c.wifi_get_status(timeout_s=2.0)
    expect(s == {"ssid": "", "rssi": ""}, f"expected empty status, got {s}")


def test_wifi_status_timeout_raises():
    """No valid response → raises (treated as 'unknown', never 'disconnected')."""
    c = make_client(on_reset=b"")  # device busy: nothing arrives
    t0 = time.time()
    try:
        c.wifi_get_status(timeout_s=0.3)
    except RuntimeError:
        expect(time.time() - t0 < 2.0, "timeout took too long")
    else:
        raise AssertionError("expected RuntimeError on timeout")


def test_status_frame_skipped_before_wifi_info():
    """STATUS frame in front of WIFI_INFO → status still parses (no flash)."""
    ssid = b"LabWiFi"
    msg = b"[NTP] sync in progress"
    frame = (bytes([0x02, len(msg)]) + msg
             + bytes([0x11, 0x01, 0x50])         # rssi=-80
             + bytes([len(ssid)]) + ssid
             + bytes([8]) + b"10.0.0.5"
             + bytes([8]) + b"10.0.0.1"
             + bytes([8]) + b"10.0.0.1")
    c = make_client(on_reset=frame)
    s = c.wifi_get_status(timeout_s=2.0)
    expect(s["ssid"] == "LabWiFi", f"ssid mismatch: {s}")


def test_scan_survives_in_transit_frame():
    """WIFI_LIST scan result still parses with a stray byte in front."""
    net = bytes([0xD0, 0x01, 6]) + b"MySSID"      # rssi=-48, PSK, ssid
    data = bytes([0x5B]) + bytes([0x10, 0x01]) + net
    c = make_client(on_reset=data)
    nets = c.wifi_scan(timeout_s=2.0)
    expect(len(nets) == 1, f"expected 1 network, got {nets}")
    expect(nets[0]["ssid"] == "MySSID", f"ssid mismatch: {nets}")
    expect(nets[0]["rssi"] == "-48", f"rssi mismatch: {nets}")


def test_fw_version_parses_string():
    """FW_VERSION reply (0x25 + length + string) parses to a version string."""
    ver = b"1.0.0"
    data = bytes([0x25, len(ver)]) + ver
    c = make_client(on_reset=data)
    r = c.fw_version(timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(r["version"] == "1.0.0", f"version mismatch: {r}")


def test_fw_version_survives_status_frame_in_front():
    """An unsolicited STATUS frame before the FW_VERSION reply is skipped."""
    msg = b"[NTP] sync in progress"
    ver = b"1.2.3"
    data = bytes([0x02, len(msg)]) + msg + bytes([0x25, len(ver)]) + ver
    c = make_client(on_reset=data)
    r = c.fw_version(timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(r["version"] == "1.2.3", f"version mismatch: {r}")


def test_fw_version_error_on_old_firmware():
    """Older firmware answers ERROR/1 (unknown command) — reported, not raised."""
    c = make_client(on_reset=bytes([0x01, 0x01]))
    r = c.fw_version(timeout_s=2.0)
    expect(r["success"] is False, f"expected failure, got {r}")
    expect("Error code 1" in r["message"], f"message mismatch: {r}")


def test_calib_timestamp_ok():
    """CALIB_TIMESTAMP (0x2A) reply OK → success; payload is [ch][u32 LE epoch]."""
    c = make_client(on_reset=bytes([0x00]))
    r = c.calib_timestamp(channel=1, epoch=1700000000, timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(c.ser.written.startswith(bytes([0x2A, 0x01])), f"cmd/ch bytes wrong: {c.ser.written!r}")
    expect(struct.pack("<I", 1700000000) in c.ser.written, "epoch bytes wrong")


def test_calib_timestamp_error_on_old_firmware():
    """Older firmware answers ERROR/1 (unknown command) — reported, not raised."""
    c = make_client(on_reset=bytes([0x01, 0x01]))
    r = c.calib_timestamp(channel=0, epoch=1700000000, timeout_s=2.0)
    expect(r["success"] is False, f"expected failure, got {r}")
    expect("Error code 1" in r["message"], f"message mismatch: {r}")


def test_eeprom_read_parses_calib_time_field():
    """EEPROM_IDS CSV gains a 5th field: decimal unix time of last calibration."""
    payload = b"123,OK,462VV,BRD-01,1700000000"
    data = bytes([0x24, len(payload)]) + payload
    c = make_client(on_reset=data)
    r = c.eeprom_read_ids(channel=0, timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(r["tgs_id"] == "123" and r["lot"] == "462VV" and r["board"] == "BRD-01",
           f"identity fields wrong: {r}")
    expect(r["calib_time"] == "1700000000", f"calib_time missing: {r}")


def test_eeprom_read_calib_time_blank_and_error():
    """Empty 5th field = never calibrated; 'ERR' = timestamp CRC bad — both parse."""
    for raw, expected in ((b"123,OK,462VV,BRD-01,", ""),
                          (b"123,OK,462VV,BRD-01,ERR", "ERR")):
        data = bytes([0x24, len(raw)]) + raw
        c = make_client(on_reset=data)
        r = c.eeprom_read_ids(channel=0, timeout_s=2.0)
        expect(r["success"] is True and r["calib_time"] == expected, f"got {r}")


def test_format_calib_time_variants():
    """_format_calib_time maps '' → (never), 'ERR' → (invalid), unix → local time."""
    f = gui.App._format_calib_time
    expect(f("") == "(never)", f"empty mapping wrong: {f('')!r}")
    expect(f("ERR") == "(invalid)", f"ERR mapping wrong: {f('ERR')!r}")
    out = f("1700000000")
    expect(out == datetime.fromtimestamp(1700000000).strftime("%Y-%m-%d %H:%M"),
           f"unix mapping wrong: {out!r}")
    expect(f("junk") == "junk", f"unparsable should pass through: {f('junk')!r}")


def test_board_id_write_ok():
    """BOARD_ID_WRITE (0x2B) reply OK → success; payload is [len][ascii id]."""
    c = make_client(on_reset=bytes([0x00]))
    r = c.board_id_write("MAIN-01", timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(c.ser.written.startswith(bytes([0x2B, 7])), f"cmd/len wrong: {c.ser.written!r}")
    expect(b"MAIN-01" in c.ser.written, "board id bytes missing")


def test_board_id_read_parses_string():
    """BOARD_ID_READ (0x2C) reply BOARD_ID (0x26) + length + string parses."""
    bid = b"MAIN-01"
    data = bytes([0x26, len(bid)]) + bid
    c = make_client(on_reset=data)
    r = c.board_id_read(timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(r["board_id"] == "MAIN-01", f"board_id wrong: {r}")


def test_board_id_read_empty_means_not_set():
    """Length 0 reply = mainboard ID never set; parses to empty string."""
    c = make_client(on_reset=bytes([0x26, 0x00]))
    r = c.board_id_read(timeout_s=2.0)
    expect(r["success"] is True and r["board_id"] == "", f"got {r}")


def test_board_id_clear_ok():
    """BOARD_ID_CLEAR (0x2D) reply OK → success."""
    c = make_client(on_reset=bytes([0x00]))
    r = c.board_id_clear(timeout_s=2.0)
    expect(r["success"] is True, f"expected success, got {r}")
    expect(c.ser.written.startswith(bytes([0x2D])), f"cmd wrong: {c.ser.written!r}")


def test_board_id_error_on_old_firmware():
    """Older firmware answers ERROR/1 (unknown command) for all three 0x2B..0x2D."""
    for fn in ("board_id_read", "board_id_clear"):
        c = make_client(on_reset=bytes([0x01, 0x01]))
        r = getattr(c, fn)(timeout_s=2.0)
        expect(r["success"] is False, f"{fn}: expected failure, got {r}")
    c = make_client(on_reset=bytes([0x01, 0x01]))
    r = c.board_id_write("X", timeout_s=2.0)
    expect(r["success"] is False, f"board_id_write: expected failure, got {r}")


def test_board_id_in_framed_response_set():
    """BOARD_ID (0x26) is in the frame-aware reader's length-framed set."""
    expect(gui.ProtoResp.BOARD_ID in gui.SerialMenuClient._LEN_FRAMED,
           "BOARD_ID missing from _LEN_FRAMED")


def test_parse_version_variants():
    """parse_version tolerates short forms and rejects non-version text."""
    expect(gui.parse_version("1.0.0") == (1, 0, 0), "1.0.0 parse failed")
    expect(gui.parse_version("2.5") == (2, 5, 0), "2.5 parse failed")
    expect(gui.parse_version(" 3.1.4-beta ") is None, "junk accepted")
    expect(gui.parse_version("") is None, "empty accepted")


def test_version_compat_rules():
    """Verdicts are exactly one of the four agreed forms.

    PATCH is invisible, MINOR is additive (compatible, flagged), MAJOR is
    breaking (incompatible) — and the verdict never carries more than that.
    """
    expect(gui.version_compat("1.0.0", "1.0.0")
           == (True, "Compatible (protocol 1)", gui.VERSION_LEVEL_OK),
           "exact match verdict wrong")

    # PATCH difference: same wire format, so the verdict is identical to a match.
    expect(gui.version_compat("1.0.3", "1.0.0")
           == (True, "Compatible (protocol 1)", gui.VERSION_LEVEL_OK),
           "patch difference should be invisible")

    # MINOR difference: compatible either direction, with the caution appended.
    for gui_v, fw_v in (("1.0.0", "1.1.0"), ("1.2.0", "1.0.9")):
        ok, text, level = gui.version_compat(gui_v, fw_v)
        expect((ok, text, level) ==
               (True, "Compatible (protocol 1, minor version differs)",
                gui.VERSION_LEVEL_WARN),
               f"minor verdict wrong for {gui_v} vs {fw_v}: {text!r}")

    # MAJOR difference: incompatible, and both protocol revisions are named.
    ok, text, level = gui.version_compat("2.0.0", "1.9.9")
    expect((ok, text, level) ==
           (False, "Incompatible (GUI protocol 2, firmware protocol 1)",
            gui.VERSION_LEVEL_ERROR),
           f"major verdict wrong: {text!r}")


def test_unavailable_version_reports_unknown_verdict():
    """An unreadable firmware version gives one verdict, whatever the cause."""
    for fw_v in ("", "abc", "1", "VGH"):
        ok, text, level = gui.version_compat(gui.GUI_VERSION, fw_v)
        expect((ok, level) == (False, gui.VERSION_LEVEL_UNKNOWN),
               f"wrong verdict for {fw_v!r}: {level}")
        expect(text == gui.VERSION_UNKNOWN_TEXT,
               f"verdict text drifted for {fw_v!r}: {text!r}")


def test_every_verdict_is_one_of_the_four_forms():
    """The About line can only ever display the four agreed message shapes."""
    import re
    allowed = re.compile(
        r"^Compatible \(protocol \d+\)$"
        r"|^Compatible \(protocol \d+, minor version differs\)$"
        r"|^Incompatible \(GUI protocol \d+, firmware protocol \d+\)$"
        r"|^Compatibility unknown \(firmware protocol unknown\)$")
    cases = [("1.0.0", "1.0.0"), ("1.0.0", "1.0.9"), ("1.0.0", "1.1.0"),
             ("1.3.2", "1.0.0"), ("2.0.0", "1.9.9"), ("1.0.0", "2.0.0"),
             ("1.0.0", ""), ("1.0.0", "abc"), ("junk", "1.0.0")]
    for gui_v, fw_v in cases:
        _ok, text, _level = gui.version_compat(gui_v, fw_v)
        expect(bool(allowed.match(text)),
               f"unexpected verdict for {gui_v} vs {fw_v!r}: {text!r}")


def test_version_levels_all_coloured():
    """Every level the checker can return has an indicator colour mapped."""
    for level in (gui.VERSION_LEVEL_OK, gui.VERSION_LEVEL_WARN,
                  gui.VERSION_LEVEL_ERROR, gui.VERSION_LEVEL_UNKNOWN):
        expect(level in gui.VERSION_LEVEL_COLOURS, f"no colour for level {level}")


def test_version_messages_have_no_status_symbols():
    """Compat verdicts are plain text: no tick/cross/warning glyphs (colour
    already conveys the verdict in the About status line)."""
    banned = "\u2713\u2717\u26a0"
    cases = [("1.0.0", "1.0.0"), ("1.0.0", "1.1.0"), ("1.2.0", "1.0.9"),
             ("2.0.0", "1.9.9"), ("1.0.0", "")]
    for gui_v, fw_v in cases:
        _ok, text, _level = gui.version_compat(gui_v, fw_v)
        expect(not any(sym in text for sym in banned),
               f"glyph left in verdict for {gui_v} vs {fw_v}: {text}")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
