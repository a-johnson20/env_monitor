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
                 Pass a list of byte strings to serve a different payload on
                 each successive reset (used for retry tests).
    """

    def __init__(self, data: bytes = b"", on_reset=b""):
        self._buf = bytearray(data)
        if isinstance(on_reset, (bytes, bytearray)):
            self._resets = [bytearray(on_reset)]
        else:
            self._resets = [bytearray(p) for p in on_reset]
        self._reset_count = 0
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
        idx = min(self._reset_count, len(self._resets) - 1)
        self._buf.extend(self._resets[idx])
        self._reset_count += 1

    def close(self) -> None:
        self.is_open = False


def make_client(data: bytes = b"", on_reset=b"") -> gui.SerialMenuClient:
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


def test_pump_get_parses_speed():
    """PUMP_STATUS is [type][len=1][percent]."""
    data = bytes([0x22, 0x01, 65])
    c = make_client(on_reset=data)
    r = c.pump_get(timeout_s=2.0)
    expect(r == {"success": True, "percent": 65}, f"unexpected result: {r}")
    expect(c.ser.written.startswith(bytes([0x22])), f"wrong command sent: {c.ser.written!r}")


def test_pump_get_tolerates_extra_fields():
    """Future PUMP_STATUS fields are consumed without affecting the speed."""
    c = make_client(on_reset=bytes([0x22, 0x03, 40, 0xAA, 0xBB]))
    r = c.pump_get(timeout_s=2.0)
    expect(r == {"success": True, "percent": 40}, f"unexpected result: {r}")


def test_pump_get_skips_status_frame_in_front():
    """A STATUS frame before the PUMP_STATUS reply is skipped."""
    msg = b"[NTP] sync in progress"
    data = bytes([0x02, len(msg)]) + msg + bytes([0x22, 0x01, 30])
    c = make_client(on_reset=data)
    r = c.pump_get(timeout_s=2.0)
    expect(r["percent"] == 30, f"percent mismatch: {r}")


def test_pump_set_sends_clamped_percent():
    """PUMP_SET payload is a single percent byte, clamped to 0-100."""
    c = make_client(on_reset=bytes([0x00]))
    r = c.pump_set(250, timeout_s=2.0)
    expect(r["success"] is True and r["percent"] == 100, f"clamp failed: {r}")
    expect(c.ser.written.startswith(bytes([0x21, 100])), f"cmd/payload wrong: {c.ser.written!r}")


def test_pump_set_error_is_reported_not_raised():
    """A device ERROR is returned as a failure dict (no exception)."""
    c = make_client(on_reset=bytes([0x01, 0x04]))
    r = c.pump_set(50, timeout_s=2.0)
    expect(r["success"] is False, f"expected failure, got {r}")
    expect("Error code 4" in r["message"], f"message mismatch: {r}")


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


_META_CSV = (b"#META,mainboard_id=MB-01,tgs2611_1_id=10,tgs2611_1_board=BRD-01,"
             b"tgs2611_1_lot=462VV,tgs2611_1_cal=2026-09-22 15:04\r\n"
             b"timestamp,tgs2611_1_raw_avg\r\n"
             b"2026-09-22 15:05:00,415\r\n"
             b"2026-09-22 15:05:10,416\r\n")


def test_preview_table_skips_meta_line():
    """_render_preview_table treats the '#META' line as a comment, not a header."""
    headers, rows, _info = gui.App._render_preview_table(_META_CSV)
    expect(headers[:2] == ["timestamp", "tgs2611_1_raw_avg"],
           f"heading row wrong: {headers}")
    expect(rows[0] == ["2026-09-22 15:05:00", "415"], f"first data row wrong: {rows}")
    expect(not any("META" in cell for row in rows for cell in row),
           "META line leaked into the preview body")


def test_parse_all_for_graphs_skips_meta_line():
    """_parse_all_for_graphs must not graph the '#META' line as a heading."""
    headers, all_rows = gui.App._parse_all_for_graphs(_META_CSV)
    expect(headers[:2] == ["timestamp", "tgs2611_1_raw_avg"],
           f"heading row wrong: {headers}")
    expect(all_rows[0][0] == "2026-09-22 15:05:00", f"first data row wrong: {all_rows}")


def test_make_csv_excel_safe_keeps_meta_line_and_fixes_timestamps():
    """Excel-safe export keeps the '#META' line and still space-prefixes data
    timestamps so Excel does not reinterpret them."""
    out = gui.App._make_csv_excel_safe(_META_CSV).decode("utf-8").lstrip("\ufeff")
    lines = out.splitlines()
    expect(lines[0].startswith("#META"), f"meta line missing: {lines[0]!r}")
    expect(lines[1].startswith("timestamp"), f"heading wrong: {lines[1]!r}")
    expect(lines[2].startswith(" 2026-09-22"), f"timestamp not space-prefixed: {lines[2]!r}")


def test_preview_table_empty_comment_only_file():
    """A file containing only the '#META' line parses to the empty-file message."""
    headers, rows, _info = gui.App._render_preview_table(b"#META,mainboard_id=MB-01\r\n")
    expect(headers == ["message"] and rows == [["(empty file)"]],
           f"expected empty-file message, got {headers} {rows}")


def test_preview_table_info_shows_meta_summary():
    """The preview info line surfaces the '#META' key/value pairs."""
    _headers, _rows, info = gui.App._render_preview_table(_META_CSV)
    expect(info.startswith("Meta: mainboard_id=MB-01"), f"meta summary missing: {info!r}")
    expect("tgs2611_1_cal=2026-09-22 15:04" in info, f"cal missing from summary: {info!r}")


def test_preview_table_info_reports_missing_meta_line():
    """A file with no metadata line says so, rather than showing a blank line.

    Files created before metadata logging (or with the line stripped) must be
    distinguishable from 'the GUI forgot to show the metadata'.
    """
    _headers, _rows, info = gui.App._render_preview_table(
        b"timestamp,tgs2611_1_raw_avg\r\n2026-09-22 15:05:00,415\r\n")
    expect("no #META line" in info, f"missing-metadata note absent: {info!r}")


def test_preview_table_info_empty_for_empty_file():
    """A completely empty file reports no info line (nothing to describe)."""
    _headers, _rows, info = gui.App._render_preview_table(b"")
    expect(info == "", f"unexpected info for empty file: {info!r}")


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


def test_preview_info_line_is_visible_above_expanding_pane():
    """The metadata info line must be shown *above* the expanding preview pane.

    Regression (reported as "I don't see the metadata"): the line was packed
    after the preview table — i.e. last inside a pane that also held an
    expanding widget — so in a short pane it was clipped away entirely. It is
    now packed before the expanding panedwindow and hidden when empty.
    """
    try:
        import tkinter  # noqa: F401
        app = gui.App()
    except Exception:
        print("      (skipped: Tk unavailable in this environment)")
        return
    app.withdraw()
    try:
        headers, rows, info = gui.App._render_preview_table(_META_CSV)
        app._set_preview_table("/logs/env_20260922.csv", headers, rows,
                               len(_META_CSV), info)
        expect(app.preview_info_var.get() == info, "info text not set on preview")
        expect("mainboard_id=MB-01" in app.preview_info_var.get(),
               f"metadata missing from info line: {app.preview_info_var.get()!r}")
        expect(app.preview_info_label.winfo_manager() == "pack",
               "metadata info line is not displayed")
        siblings = app.preview_info_label.master.pack_slaves()
        expect(siblings.index(app.preview_info_label) < siblings.index(app.inner_split),
               "info line packed after the expanding pane (gets clipped)")

        # While a preview is loading the line is hidden, freeing the space.
        app._set_preview_message("/logs/env_20260922.csv", "Loading preview...", 0)
        expect(app.preview_info_var.get() == "", "info text not cleared")
        expect(app.preview_info_label.winfo_manager() == "",
               "info line still occupies space while loading")

        # ...and can be shown again afterwards (pack/unpack is repeatable).
        app._set_preview_info(info)
        expect(app.preview_info_label.winfo_manager() == "pack",
               "info line did not reappear after being hidden")
    finally:
        app.destroy()


def _log_list_frame(files: list[tuple[int, str]]) -> bytes:
    """Build a LOG_LIST (0x13) frame: count + per file [size u32 LE][len][path]."""
    out = bytearray([0x13, len(files)])
    for size, path in files:
        p = path.encode()
        out += struct.pack("<I", size) + bytes([len(p)]) + p
    return bytes(out)


def test_log_list_parses_files():
    """LOG_LIST parses size (u32 LE) + length-prefixed path per entry."""
    c = make_client(on_reset=_log_list_frame([(1234, "/logs/env_20260922.csv")]))
    files = c.list_logs(timeout_s=2.0)
    expect(len(files) == 1, f"expected 1 file, got {files}")
    expect(files[0].path == "/logs/env_20260922.csv", f"path wrong: {files[0].path!r}")
    expect(files[0].size == 1234, f"size wrong: {files[0].size}")
    expect(not c.ser.written.startswith(bytes([0x13])), "wrong command sent")


def test_log_list_sd_error_retries_then_succeeds():
    """A transient SD_ERROR (code 2) is retried once; the retry's file list wins.

    This is the 'device error 2 on refresh' report: the card was momentarily
    unavailable (mid-remount), so the second request succeeds.
    """
    resets = [bytes([0x01, 0x02]),                     # ERROR / SD_ERROR
              _log_list_frame([(99, "/logs/a.csv")])]
    c = make_client(on_reset=resets)
    files = c.list_logs(timeout_s=2.0)
    expect(len(files) == 1 and files[0].size == 99, f"retry did not recover: {files}")
    expect(c.ser.written.count(0x1C) == 2, f"expected 2 LOG_LIST sends: {c.ser.written!r}")


def test_log_list_sd_error_reports_readable_message():
    """A persistent SD_ERROR raises with a descriptive message, not 'error 2'."""
    c = make_client(on_reset=bytes([0x01, 0x02]))
    try:
        c.list_logs(timeout_s=2.0)
    except RuntimeError as exc:
        msg = str(exc)
        expect("Device error 2" in msg, f"code missing from message: {msg!r}")
        expect("SD card" in msg, f"message not descriptive: {msg!r}")
    else:
        raise AssertionError("expected RuntimeError on persistent SD error")


def test_log_list_other_errors_are_not_retried():
    """Non-SD errors (e.g. invalid index) fail immediately — one request only."""
    c = make_client(on_reset=bytes([0x01, 0x05]))
    try:
        c.list_logs(timeout_s=2.0)
    except RuntimeError as exc:
        expect("invalid index" in str(exc), f"message wrong: {exc}")
    else:
        raise AssertionError("expected RuntimeError")
    expect(c.ser.written.count(0x1C) == 1, f"should not retry: {c.ser.written!r}")


def test_describe_device_error_mapping():
    """Every firmware error code maps to a readable label; unknown codes survive."""
    for code in range(1, 9):
        msg = gui.SerialMenuClient._describe_device_error(code)
        expect(msg.startswith(f"Device error {code} ("), f"code {code} unlabelled: {msg!r}")
    expect(gui.SerialMenuClient._describe_device_error(99) == "Device error 99",
           "unknown code should still be reported")


def test_stale_log_begin_frame_does_not_desync_file_list():
    """A stale LOG_BEGIN whose payload contains 0x13 must not be mistaken for the
    LOG_LIST reply (byte-wise draining would read its size byte as the response)."""
    stale = bytes([0x14]) + struct.pack("<I", 0x13) + bytes([4]) + b"/abc"
    data = stale + _log_list_frame([(7, "/logs/real.csv")])
    c = make_client(on_reset=data)
    files = c.list_logs(timeout_s=2.0)
    expect(len(files) == 1 and files[0].path == "/logs/real.csv",
           f"stale frame desynced the list: {files}")


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
