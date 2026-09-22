"""Regression tests for the GUI's frame-aware serial protocol reader.

These run against a FakeSerial — no device or COM port needed:

    python test/gui_protocol_test.py

Covers the WiFi-connect failure modes reported in the field:
  * "Unexpected response: 91"  (a stray payload byte read as a response type)
  * "WiFi: Disconnected" flashing right after a successful connect
    (device busy frames / late replies misparsed as a disconnect report)
"""

import sys
import time
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
