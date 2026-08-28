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
