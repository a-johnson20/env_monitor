#!/usr/bin/env python3
"""
Simple desktop GUI for env_monitor serial menu.

Features:
- Connect/disconnect to serial port
- Start/stop live data stream (menu option 1)
- List log files from SD (menu option 3)
- Download selected log file as CSV

Requires:
- pyserial
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk

import serial
from serial.tools import list_ports
try:
    import ttkbootstrap as tb
    HAS_TTKBOOTSTRAP = True
except Exception:
    tb = None
    HAS_TTKBOOTSTRAP = False

# Try to disable DPI scaling awareness on Windows to fix blurriness
try:
    import ctypes
    # Try per-monitor DPI awareness (most aggressive)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        # Fall back to system DPI awareness
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
except Exception:
    pass  # Non-Windows or older Windows version

# Load WiFi icon font if available
def get_font_path() -> Path | None:
    """Find the WiFi ramp font, checking bundled and development locations."""
    # When running as PyInstaller executable
    if getattr(sys, 'frozen', False):
        font_path = Path(sys._MEIPASS) / "fonts" / "DejaVuSansMono-wifi-ramp.ttf"
        if font_path.exists():
            return font_path
    
    # Development/repo location
    repo_font = Path(__file__).parent.parent / "fonts" / "DejaVuSansMono-wifi-ramp.ttf"
    if repo_font.exists():
        return repo_font
    
    return None

WIFI_FONT_PATH = get_font_path()
WIFI_FONT_NAME = "DejaVu Sans Mono wifi ramp"

# Try to register the WiFi font on Windows
if WIFI_FONT_PATH and hasattr(ctypes, 'windll'):
    try:
        import ctypes
        # Add font to registry for current session
        ctypes.windll.gdi32.AddFontResourceExW(str(WIFI_FONT_PATH), 0x10, 0)
    except Exception:
        pass  # Font loading failed, will fall back to default


LIST_RE = re.compile(r"^\s*(\d+)\)\s+(.+)\s+\((\d+)\s+bytes\)\s*$")
BEGIN_RE = re.compile(r"^BEGIN_LOG\s+(.+)\s+(\d+)\s*$")
END_RE = re.compile(r"^END_LOG\s+(.+)\s+(\d+)\s*$")
LIVE_HEADER_PREFIX = "LIVE_HEADER "


@dataclass
class FileEntry:
    index: int
    path: str
    size: int


class SerialMenuClient:
    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.live_thread: threading.Thread | None = None
        self.live_stop = threading.Event()

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self, port: str, baud: int) -> None:
        if self.is_open:
            self.close()
        self.ser = serial.Serial(port, baud, timeout=0.1, write_timeout=2)
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        self.stop_live()
        with self.lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                finally:
                    self.ser = None

    def _require_open(self) -> serial.Serial:
        if not self.is_open or self.ser is None:
            raise RuntimeError("Serial port is not open")
        return self.ser

    @staticmethod
    def _read_line_with_deadline(ser: serial.Serial, deadline: float) -> str | None:
        buf = bytearray()
        while time.time() < deadline:
            b = ser.read(1)
            if not b:
                continue
            if b == b"\n":
                return buf.decode("utf-8", errors="replace").strip("\r")
            buf.extend(b)
        return None

    @staticmethod
    def _read_menu_lines_until_ready(
        ser: serial.Serial,
        timeout_s: float,
        idle_s: float = 0.35,
    ) -> list[str]:
        """
        Read menu text until we detect prompt bytes ("> " / ">") or quiet idle.
        The firmware prints prompt without newline, so line-based reads alone
        can otherwise wait until timeout.
        """
        deadline = time.time() + timeout_s
        last_rx = time.time()
        saw_any = False
        lines: list[str] = []
        buf = bytearray()

        while time.time() < deadline:
            b = ser.read(1)
            if b:
                saw_any = True
                last_rx = time.time()
                buf.extend(b)

                # Drain complete lines.
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw = bytes(buf[:nl])
                    del buf[: nl + 1]
                    lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))

                # Prompt is printed with Serial.print("> "), so detect raw tail.
                if buf.endswith(b"> ") or buf.endswith(b">"):
                    return lines
            else:
                if saw_any and (time.time() - last_rx) >= idle_s:
                    return lines

        return lines

    @staticmethod
    def _read_log_export_lines_until_ready(
        ser: serial.Serial,
        timeout_s: float,
        idle_s: float = 0.35,
    ) -> list[str]:
        """
        Read serial output until the Log Export menu prompt is reached.
        Ignore earlier prompts from other menus.
        """
        deadline = time.time() + timeout_s
        last_rx = time.time()
        buf = bytearray()
        saw_section = False
        section_lines: list[str] = []

        while time.time() < deadline:
            b = ser.read(1)
            if b:
                last_rx = time.time()
                buf.extend(b)

                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    raw = bytes(buf[:nl])
                    del buf[: nl + 1]
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")

                    if (
                        "=== Log Export (Serial) ===" in line
                        or LIST_RE.match(line) is not None
                        or "Choose file number to stream." in line
                    ):
                        saw_section = True

                    if saw_section:
                        section_lines.append(line)

                # Prompt may be printed without newline.
                if saw_section and (buf.endswith(b"> ") or buf.endswith(b">")):
                    return section_lines
            else:
                # If we already saw section content and serial is quiet, return.
                if saw_section and (time.time() - last_rx) >= idle_s:
                    return section_lines

        if saw_section:
            return section_lines
        raise RuntimeError("Timed out waiting for Log Export menu")

    def _send_line(self, line: str) -> None:
        ser = self._require_open()
        ser.write((line + "\n").encode("utf-8"))
        ser.flush()

    def _goto_main_menu(self) -> None:
        # Best-effort return to root menu.
        self._send_line("b")
        time.sleep(0.15)
        self._send_line("b")
        time.sleep(0.15)

    def list_logs(self, timeout_s: float = 20.0) -> list[FileEntry]:
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before listing files")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("3")
            try:
                lines = self._read_log_export_lines_until_ready(ser, timeout_s=timeout_s, idle_s=0.35)
                files: list[FileEntry] = []
                for line in lines:
                    m = LIST_RE.match(line)
                    if m:
                        files.append(
                            FileEntry(
                                index=int(m.group(1)),
                                path=m.group(2),
                                size=int(m.group(3)),
                            )
                        )
                return files
            finally:
                # Leave export menu so firmware can resume SD logging.
                self._send_line("b")

    @staticmethod
    def _read_exact(ser: serial.Serial, n: int, timeout_s: float) -> bytes:
        deadline = time.time() + timeout_s
        out = bytearray()
        while len(out) < n and time.time() < deadline:
            chunk = ser.read(n - len(out))
            if not chunk:
                continue
            out.extend(chunk)
        if len(out) != n:
            raise RuntimeError(f"Timed out reading payload: expected {n}, got {len(out)}")
        return bytes(out)

    def download_log_bytes(self, index: int, timeout_s: float = 20.0) -> tuple[str, bytes]:
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before downloading files")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("3")
            try:
                # Wait for export menu to be ready (prompt is not newline-terminated).
                _ = self._read_log_export_lines_until_ready(ser, timeout_s=timeout_s, idle_s=0.35)

                self._send_line(str(index))

                # Find BEGIN_LOG
                begin_path = ""
                begin_size = -1
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    line = self._read_line_with_deadline(ser, deadline)
                    if line is None:
                        break
                    m = BEGIN_RE.match(line)
                    if m:
                        begin_path = m.group(1)
                        begin_size = int(m.group(2))
                        break
                if begin_size < 0:
                    raise RuntimeError("BEGIN_LOG header not found")

                payload = self._read_exact(ser, begin_size, max(timeout_s, begin_size / 2048.0))

                # After payload, firmware sends newline and END_LOG
                _ = self._read_line_with_deadline(ser, time.time() + timeout_s)
                end_path = ""
                end_size = -1
                deadline = time.time() + timeout_s
                while time.time() < deadline:
                    line = self._read_line_with_deadline(ser, deadline)
                    if line is None:
                        break
                    m = END_RE.match(line)
                    if m:
                        end_path = m.group(1)
                        end_size = int(m.group(2))
                        break
                if end_size < 0:
                    raise RuntimeError("END_LOG footer not found")

                if begin_path != end_path or begin_size != end_size:
                    raise RuntimeError(
                        f"Transfer metadata mismatch: BEGIN({begin_path},{begin_size}) END({end_path},{end_size})"
                    )
                if len(payload) != end_size:
                    raise RuntimeError(f"Payload mismatch: {len(payload)} bytes vs {end_size}")

                return begin_path, payload
            finally:
                # Leave export menu so firmware can resume SD logging.
                self._send_line("b")

    def download_log(self, index: int, output_path: Path, timeout_s: float = 20.0) -> int:
        _, payload = self.download_log_bytes(index=index, timeout_s=timeout_s)
        output_path.write_bytes(payload)
        return len(payload)

    def start_live(self, on_line) -> None:
        if self.live_thread is not None and self.live_thread.is_alive():
            return
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("1")

        self.live_stop.clear()
        self.live_thread = threading.Thread(
            target=self._live_loop,
            args=(on_line,),
            daemon=True,
        )
        self.live_thread.start()

    def _live_loop(self, on_line) -> None:
        ser = self._require_open()
        buf = bytearray()
        while not self.live_stop.is_set():
            b = ser.read(1)
            if not b:
                continue
            if b == b"\n":
                line = buf.decode("utf-8", errors="replace").strip("\r")
                buf.clear()
                if line and "," in line:
                    on_line(line)
                continue
            buf.extend(b)

        # Firmware exits live mode when it receives raw 'b'
        try:
            ser.write(b"b")
            ser.flush()
        except Exception:
            pass

    def stop_live(self) -> None:
        if self.live_thread is None:
            return
        self.live_stop.set()
        self.live_thread.join(timeout=2.0)
        self.live_thread = None

    def wifi_scan(self, timeout_s: float = 15.0) -> list[dict]:
        """Scan for available WiFi networks and return list of networks."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu option
            time.sleep(0.5)
            self._send_line("1")  # Scan networks option
            time.sleep(1.0)  # Give device time to scan

            # Read until scan is complete
            deadline = time.time() + timeout_s
            networks: list[dict] = []
            buf = bytearray()
            saw_header = False

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue

                buf.extend(b)

                # Look for complete lines
                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    # Skip empty lines
                    if not line:
                        continue

                    # Look for the header line
                    if "#  RSSI  SEC  SSID" in line or "#  RSSI" in line:
                        saw_header = True
                        continue

                    # Stop if we've seen the header and now hit the prompt/menu
                    if saw_header and (line.startswith("> ") or line == ">" or "Select a network" in line):
                        break

                    # Parse network entries after we've seen the header
                    # Format: "1) -45  WPA2-PSK  MyNetwork"
                    if saw_header and ") " in line[:3]:
                        try:
                            # Split on first ")" to separate index from rest
                            parts = line.split(")", 1)
                            if len(parts) == 2:
                                remainder = parts[1].strip()
                                # Split on whitespace to get RSSI, SEC, and SSID
                                tokens = remainder.split(None, 2)  # Split on whitespace, max 3 parts
                                if len(tokens) >= 3:
                                    rssi_str = tokens[0]
                                    sec_str = tokens[1]
                                    ssid_str = tokens[2]

                                    # Validate RSSI looks like a number (negative dBm)
                                    try:
                                        rssi_val = int(rssi_str)
                                        # Normalize OPEN to None for consistency
                                        sec_display = "None" if sec_str == "OPEN" else sec_str
                                        networks.append({
                                            "ssid": ssid_str,
                                            "rssi": rssi_str,
                                            "security": sec_display
                                        })
                                    except ValueError:
                                        pass
                        except (IndexError, ValueError):
                            pass

            return networks

    def wifi_connect(self, ssid: str, password: str, auth_type: str = "PSK", username: str = "", timeout_s: float = 20.0) -> dict:
        """Connect to a WiFi network."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)

            # Choose connect method based on auth type
            if auth_type == "WPA2-EAP":
                self._send_line("3")  # WPA2-EAP option
            else:
                self._send_line("2")  # PSK option

            time.sleep(0.3)

            # Send SSID
            self._send_line(ssid)
            time.sleep(0.3)

            # Send credentials based on auth type
            if auth_type == "WPA2-EAP" and username:
                # For EAP, send username first
                self._send_line(username)
                time.sleep(0.3)
            
            # Send password
            self._send_line(password)
            time.sleep(0.5)

            # Read response
            deadline = time.time() + timeout_s
            response_lines: list[str] = []
            buf = bytearray()

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    response_lines.append(line)
                    buf = bytearray()

                    if "success" in line.lower() or "connected" in line.lower():
                        return {"success": True, "ssid": ssid, "message": line}
                    elif "fail" in line.lower() or "error" in line.lower():
                        return {"success": False, "ssid": ssid, "message": line}

            return {"success": False, "ssid": ssid, "message": "Timeout waiting for response"}

    def wifi_disconnect(self, timeout_s: float = 10.0) -> dict:
        """Disconnect WiFi."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)
            self._send_line("5")  # Disconnect option
            time.sleep(0.5)

            # Read response
            deadline = time.time() + timeout_s
            buf = bytearray()

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    if "disconnect" in line.lower() or "success" in line.lower():
                        return {"success": True, "message": line}

            return {"success": True, "message": "Disconnect command sent"}

    def wifi_reset(self, timeout_s: float = 10.0) -> dict:
        """Reset/forget all WiFi networks."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)
            self._send_line("6")  # Reset option
            time.sleep(0.5)

            # Read response
            deadline = time.time() + timeout_s
            buf = bytearray()

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    if "reset" in line.lower() or "forget" in line.lower() or "success" in line.lower():
                        return {"success": True, "message": line}

            return {"success": True, "message": "Reset command sent"}

    def wifi_get_status(self, timeout_s: float = 5.0) -> dict:
        """Get current WiFi status (SSID and RSSI)."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu

            # Read until we get the WiFi menu output which shows connection status
            deadline = time.time() + timeout_s
            buf = bytearray()
            ssid = ""
            rssi = ""

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    # Look for WiFi connection info in output
                    # Handle ESP32 format: "[WiFi] SSID=X  IP=...  GW=...  DNS0=...  RSSI=-45"
                    if "[WiFi]" in line and "SSID=" in line:
                        try:
                                    # Extract SSID - capture everything after SSID= until multiple spaces or IP=
                            ssid_match = re.search(r'SSID=(.+?)\s{2,}', line)
                            if ssid_match:
                                ssid = ssid_match.group(1).strip()
                            
                            # Extract RSSI - look for "RSSI=" followed by a number
                            rssi_match = re.search(r'RSSI=(-?\d+)', line)
                            if rssi_match:
                                rssi = rssi_match.group(1)
                        except (IndexError, ValueError):
                            pass

                    # Stop when we see the prompt/menu
                    if ")==>" in line or line.endswith("> "):
                        break

            # Go back to main menu
            self._send_line("b")

            return {"ssid": ssid, "rssi": rssi}

    def wifi_get_saved_networks(self, timeout_s: float = 5.0) -> list[str]:
        """Get list of saved WiFi networks."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)
            self._send_line("4")  # Saved networks option

            # Read until we get the saved networks list
            deadline = time.time() + timeout_s
            buf = bytearray()
            networks: list[str] = []

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    # Parse saved networks: "1) SSID [WPA2-PSK]"
                    if re.match(r'^\d+\)\s+.+\s+\[.+\]$', line):
                        # Extract just the SSID part (between ") " and " [")
                        match = re.match(r'^\d+\)\s+(.+?)\s+\[.+\]', line)
                        if match:
                            ssid = match.group(1)
                            networks.append(ssid)

                    # Stop when we see the prompt
                    if ")==>" in line or ("Select" in line and "back" in line):
                        break

            # Go back to main menu
            self._send_line("b")

            return networks

    def wifi_connect_saved(self, ssid: str, timeout_s: float = 20.0) -> dict:
        """Connect to a saved WiFi network by SSID."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)
            self._send_line("4")  # Saved networks option
            time.sleep(0.3)

            # Find the network index
            deadline = time.time() + 5.0
            buf = bytearray()
            network_index = -1
            network_count = 0

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    # Parse saved networks: "1) SSID [WPA2-PSK]"
                    match = re.match(r'^(\d+)\)\s+(.+?)\s+\[.+\]', line)
                    if match:
                        network_count += 1
                        idx = int(match.group(1))
                        found_ssid = match.group(2)
                        if found_ssid == ssid:
                            network_index = idx
                            break

                    # Stop if we see the prompt before finding the network
                    if ")==>" in line or ("Select" in line and "back" in line):
                        break

            if network_index < 0:
                return {"success": False, "ssid": ssid, "message": "Network not found in saved list"}

            # Select the network
            self._send_line(str(network_index))
            time.sleep(0.3)

            # Connect to it (option 1)
            self._send_line("1")
            time.sleep(0.5)

            # Read response
            deadline = time.time() + timeout_s
            response_lines: list[str] = []
            buf = bytearray()

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    response_lines.append(line)
                    buf = bytearray()

                    if "connected" in line.lower() or "success" in line.lower():
                        return {"success": True, "ssid": ssid, "message": line}
                    elif "fail" in line.lower() or "error" in line.lower():
                        return {"success": False, "ssid": ssid, "message": line}

            return {"success": False, "ssid": ssid, "message": "Timeout waiting for response"}

    def wifi_forget_by_ssid(self, ssid: str, timeout_s: float = 10.0) -> dict:
        """Forget (delete) a saved WiFi network by SSID."""
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._goto_main_menu()
            self._send_line("2")  # WiFi menu
            time.sleep(0.3)
            self._send_line("4")  # Saved networks option
            time.sleep(0.3)

            # Find the network index
            deadline = time.time() + 5.0
            buf = bytearray()
            network_index = -1

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    # Parse saved networks: "1) SSID [WPA2-PSK]"
                    match = re.match(r'^(\d+)\)\s+(.+?)\s+\[.+\]', line)
                    if match:
                        idx = int(match.group(1))
                        found_ssid = match.group(2)
                        if found_ssid == ssid:
                            network_index = idx
                            break

                    # Stop if we see the prompt before finding the network
                    if ")==>" in line or ("Select" in line and "back" in line):
                        break

            if network_index < 0:
                return {"success": False, "ssid": ssid, "message": "Network not found in saved list"}

            # Select the network
            self._send_line(str(network_index))
            time.sleep(0.3)

            # Forget it (option 2)
            self._send_line("2")
            time.sleep(0.3)

            # Read response
            deadline = time.time() + timeout_s
            buf = bytearray()

            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                buf.extend(b)

                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = bytearray()

                    if "deleted" in line.lower() or "success" in line.lower():
                        return {"success": True, "ssid": ssid, "message": line}
                    elif "fail" in line.lower() or "error" in line.lower():
                        return {"success": False, "ssid": ssid, "message": line}

            return {"success": False, "ssid": ssid, "message": "Timeout waiting for response"}

    def get_rtc_time(self, timeout_s: float = 3.0) -> dict:
        """Get the current RTC time from the device via menu option 4.
        
        Returns a dict with 'time' key containing time string in HH:MM (no seconds),
        and 'success' key indicating if the query was successful.
        """
        if not self.is_open:
            raise RuntimeError("Serial port is not open")

        try:
            with self.lock:
                ser = self.ser
                if not ser or not ser.is_open:
                    return {"success": False, "time": "---- -- --:--"}

                # Send menu option 4 to show RTC
                ser.write(b"4\n")
                ser.flush()

                # Read response, looking for "RTC: YYYY-MM-DD HH:MM" or "RTC: Not available"
                import re
                buf = bytearray()
                start_time = time.time()
                
                while time.time() - start_time < timeout_s:
                    b = ser.read(1)
                    if not b:
                        continue
                    buf.extend(b)

                    if buf.endswith(b"\n"):
                        line = buf.decode("utf-8", errors="replace").strip()
                        buf = bytearray()

                        if line.startswith("RTC:"):
                            # Parse "RTC: YYYY-MM-DD HH:MM" format
                            match = re.search(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", line)
                            if match:
                                year, month, day, hour, minute = match.groups()
                                time_str = f"{year}-{month}-{day} {hour}:{minute}"
                                return {"success": True, "time": time_str}
                            elif "Not available" in line:
                                return {"success": False, "time": "---- -- --:--"}

                return {"success": False, "time": "---- -- --:--"}
        except Exception as e:
            return {"success": False, "time": "---- -- --:--"}


class LiveGraphsWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Live Graphs")
        self.geometry("980x760")
        self.minsize(760, 520)
        self.c_bg = getattr(master, "c_bg", "#eef2f8")
        self.c_surface = getattr(master, "c_surface", "#ffffff")
        self.c_border = getattr(master, "c_border", "#d4dbe6")
        self.configure(bg=self.c_bg)

        self.status_var = tk.StringVar(value="Waiting for live data...")
        ttk.Label(self, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w", padx=10, pady=(8, 4))

        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.scroll_canvas = tk.Canvas(outer, highlightthickness=0, bg=self.c_bg)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.scroll_canvas.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.scroll_canvas.configure(yscrollcommand=vsb.set)

        self.content = ttk.Frame(self.scroll_canvas)
        self.content_id = self.scroll_canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_content_configure)
        self.scroll_canvas.bind("<Configure>", self._on_canvas_configure)

        self.graph_names: list[str] = []
        self.graph_canvases: list[tk.Canvas] = []

        self._last_headers: list[str] = []
        self._last_x: list[float] = []
        self._last_labels: list[str] = []
        self._last_series: list[list[float | None]] = []

    def _on_content_configure(self, _event=None) -> None:
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.scroll_canvas.itemconfigure(self.content_id, width=event.width)
        self.redraw()

    def _rebuild_graph_widgets(self, var_names: list[str]) -> None:
        for child in self.content.winfo_children():
            child.destroy()

        self.graph_names = list(var_names)
        self.graph_canvases = []

        for name in var_names:
            pane = ttk.LabelFrame(self.content, text=name)
            pane.pack(fill=tk.X, padx=8, pady=6)
            canvas = tk.Canvas(
                pane,
                height=170,
                bg=self.c_surface,
                highlightthickness=1,
                highlightbackground=self.c_border,
            )
            canvas.pack(fill=tk.X, expand=True, padx=6, pady=6)
            canvas.bind("<Configure>", lambda _e: self.redraw())
            self.graph_canvases.append(canvas)

    def render(
        self,
        headers: list[str],
        x_values: list[float],
        time_labels: list[str],
        series: list[list[float | None]],
    ) -> None:
        self._last_headers = list(headers)
        self._last_x = list(x_values)
        self._last_labels = list(time_labels)
        self._last_series = [list(s) for s in series]

        var_names = headers[1:] if len(headers) > 1 else []
        if var_names != self.graph_names:
            self._rebuild_graph_widgets(var_names)

        if not var_names:
            self.status_var.set("Waiting for live data...")
        else:
            self.status_var.set(f"Showing {len(x_values)} point(s) per variable")

        self.redraw()

    def redraw(self) -> None:
        for i, canvas in enumerate(self.graph_canvases):
            y_vals = self._last_series[i] if i < len(self._last_series) else []
            self._draw_series(canvas, self._last_x, self._last_labels, y_vals)

    @staticmethod
    def _draw_series(
        canvas: tk.Canvas,
        x_values: list[float],
        time_labels: list[str],
        y_values: list[float | None],
    ) -> None:
        canvas.delete("all")
        w = max(canvas.winfo_width(), 320)
        h = max(canvas.winfo_height(), 120)
        left, top, right, bottom = 56, 10, 12, 28
        x0, y0 = left, top
        x1, y1 = w - right, h - bottom
        canvas.create_rectangle(x0, y0, x1, y1, outline="#b8b8b8")

        n = min(len(x_values), len(time_labels), len(y_values))
        if n < 2:
            canvas.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text="Waiting for more data...",
                fill="#666666",
            )
            return

        xs = x_values[-n:]
        ts = time_labels[-n:]
        ys = y_values[-n:]
        valid = [i for i, v in enumerate(ys) if v is not None]
        if len(valid) < 2:
            canvas.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text="No numeric data points yet",
                fill="#666666",
            )
            return

        x_min, x_max = xs[0], xs[-1]
        if x_max <= x_min:
            x_max = x_min + 1.0

        y_valid = [ys[i] for i in valid if ys[i] is not None]
        y_min, y_max = min(y_valid), max(y_valid)
        if y_max <= y_min:
            span = abs(y_min) * 0.05
            if span < 0.5:
                span = 0.5
            y_min -= span
            y_max += span

        def map_x(xv: float) -> float:
            return x0 + (xv - x_min) * (x1 - x0) / (x_max - x_min)

        def map_y(yv: float) -> float:
            return y1 - (yv - y_min) * (y1 - y0) / (y_max - y_min)

        # Light horizontal guides.
        for g in range(1, 4):
            gy = y0 + g * (y1 - y0) / 4
            canvas.create_line(x0, gy, x1, gy, fill="#f0f0f0")

        # Draw line segments, breaking on missing values.
        segment: list[float] = []
        for i in range(n):
            yv = ys[i]
            if yv is None:
                if len(segment) >= 4:
                    canvas.create_line(*segment, fill="#1f77b4", width=2)
                segment = []
                continue
            segment.extend((map_x(xs[i]), map_y(yv)))
        if len(segment) >= 4:
            canvas.create_line(*segment, fill="#1f77b4", width=2)

        canvas.create_text(x0 - 4, y0, text=f"{y_max:.4g}", anchor="ne", fill="#555555")
        canvas.create_text(x0 - 4, y1, text=f"{y_min:.4g}", anchor="se", fill="#555555")
        canvas.create_text(x0, y1 + 14, text=ts[0], anchor="w", fill="#555555")
        canvas.create_text(x1, y1 + 14, text=ts[-1], anchor="e", fill="#555555")


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("GEM GUI")
        # Reduce initial size to account for DPI scaling and prevent blurriness
        self.geometry("900x680")
        self.minsize(850, 620)

        self.c_bg = "#eef2f8"
        self.c_surface = "#ffffff"
        self.c_text = "#1f2937"
        self.c_muted = "#5b6678"
        self.c_accent = "#0b66d6"
        self.c_accent_hover = "#0a58bc"
        self.c_accent_press = "#084b9e"
        self.c_border = "#d4dbe6"
        self.c_row_alt = "#f7f9fc"
        self.c_tab_idle = "#e3e9f3"

        self.client = SerialMenuClient()
        self.events: Queue[tuple[str, object]] = Queue()
        self.file_entries: list[FileEntry] = []
        self.latest_fields: list[str] = []
        self.preview_cache: dict[int, tuple[str, list[str], list[list[str]], int, str]] = {}
        self.connected = False
        self.live_running = False
        self.files_refresh_inflight = False
        self.preview_loading_index: int | None = None
        self.live_history_limit = 600
        self.live_sample_counter = 0
        self.live_headers: list[str] = ["timestamp"]
        self.live_x_values: deque[float] = deque(maxlen=self.live_history_limit)
        self.live_time_labels: deque[str] = deque(maxlen=self.live_history_limit)
        self.live_series: list[deque[float | None]] = []
        self.live_graphs_window: LiveGraphsWindow | None = None

        # WiFi settings
        self.wifi_scan_cache: list[dict] = []
        self.wifi_saved_networks: list[str] = []
        self.wifi_scan_inflight = False
        self.wifi_poll_inflight = False
        self.wifi_poll_timer: int | None = None
        self.wifi_viewing_saved = False  # Track if viewing saved networks list
        self.wifi_current_ssid = ""
        self.wifi_current_rssi = ""
        self.wifi_current_form_type: str | None = None
        self.wifi_current_form_ssid: str = ""
        self.wifi_form_password: ttk.Entry | None = None
        self.wifi_form_username: ttk.Entry | None = None
        self.wifi_form_ssid: ttk.Entry | None = None
        self.wifi_form_security: ttk.Combobox | None = None
        self.wifi_form_fields_frame: ttk.Frame | None = None
        
        # RTC settings
        self.rtc_poll_timer: int | None = None
        self.rtc_poll_inflight = False
        self.rtc_current_datetime = "---- -- --:--"

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.status_var = tk.StringVar(value="Disconnected")
        self.preview_title_var = tk.StringVar(value="Preview")
        self.preview_info_var = tk.StringVar(value="")
        self.connected_port: str | None = None
        self.port_display_to_device: dict[str, str] = {}

        self._configure_theme()
        self._build_ui()
        self._schedule_poll()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _set_port_combo_state(self) -> None:
        # Prevent selecting a different COM port while an active connection exists.
        self.port_combo.configure(state=tk.DISABLED if self.connected else "readonly")

    def _on_port_selected(self, _event=None) -> None:
        # Remove selection highlight in the readonly field after choice.
        try:
            self.port_combo.selection_clear()
        except Exception:
            pass
        self.focus_set()

    def _on_port_focus_in(self, _event=None) -> None:
        try:
            self.port_combo.selection_clear()
        except Exception:
            pass

    def _configure_theme(self) -> None:
        if HAS_TTKBOOTSTRAP and tb is not None:
            self.style = tb.Style(theme="flatly")
            colors = getattr(self.style, "colors", None)
            if colors is not None:
                self.c_bg = getattr(colors, "bg", self.c_bg)
                self.c_surface = getattr(colors, "light", self.c_surface)
                self.c_text = getattr(colors, "fg", self.c_text)
                self.c_muted = getattr(colors, "secondary", self.c_muted)
                self.c_accent = getattr(colors, "primary", self.c_accent)
                self.c_accent_hover = self.c_accent
                self.c_accent_press = self.c_accent
                self.c_border = getattr(colors, "border", self.c_border)
                self.c_row_alt = getattr(colors, "bg", self.c_row_alt)
        else:
            self.style = ttk.Style(self)
            available = set(self.style.theme_names())
            for candidate in ("clam", "vista", "xpnative", "default"):
                if candidate in available:
                    self.style.theme_use(candidate)
                    break

        self.configure(bg=self.c_bg)

        self.style.configure(".", font=("Segoe UI", 10))
        self.style.configure("TFrame", background=self.c_bg)
        self.style.configure("TLabel", background=self.c_bg, foreground=self.c_text)
        self.style.configure("Section.TLabel", background=self.c_bg, foreground=self.c_text, font=("Segoe UI Semibold", 10))
        self.style.configure("Muted.TLabel", background=self.c_bg, foreground=self.c_muted)

        self.style.configure("TButton", padding=(10, 6), font=("Segoe UI", 10))
        self.style.configure(
            "Accent.TButton",
            padding=(10, 6),
            font=("Segoe UI Semibold", 10),
            foreground="#ffffff",
            background=self.c_accent,
            borderwidth=0,
        )
        self.style.map(
            "Accent.TButton",
            background=[
                ("disabled", "#a8b5cc"),
                ("pressed", self.c_accent_press),
                ("active", self.c_accent_hover),
            ],
            foreground=[("disabled", "#ecf2ff")],
        )

        self.style.configure("TEntry", padding=5)
        self.style.configure("TCombobox", padding=4)
        # Make dropdown selection less visually loud.
        self.option_add("*TCombobox*Listbox.background", self.c_surface)
        self.option_add("*TCombobox*Listbox.foreground", self.c_text)
        self.option_add("*TCombobox*Listbox.selectBackground", self.c_surface)
        self.option_add("*TCombobox*Listbox.selectForeground", self.c_text)
        self.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")

        self.style.configure("TNotebook", background=self.c_bg, borderwidth=0, tabmargins=(2, 0, 2, 0))
        self.style.configure("TNotebook.Tab", padding=(14, 8), font=("Segoe UI Semibold", 10))
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", self.c_surface), ("!selected", self.c_tab_idle)],
            foreground=[("selected", self.c_text), ("!selected", self.c_muted)],
        )

        self.style.configure("TPanedwindow", background=self.c_bg)
        self.style.configure("TLabelframe", background=self.c_bg, bordercolor=self.c_border)
        self.style.configure("TLabelframe.Label", background=self.c_bg, foreground=self.c_text, font=("Segoe UI Semibold", 10))

        self.style.configure(
            "Treeview",
            background=self.c_surface,
            fieldbackground=self.c_surface,
            foreground=self.c_text,
            rowheight=24,
            bordercolor=self.c_border,
            lightcolor=self.c_border,
            darkcolor=self.c_border,
        )
        self.style.configure(
            "Treeview.Heading",
            background="#e6ecf5",
            foreground=self.c_text,
            relief="flat",
            padding=(6, 6),
            font=("Segoe UI Semibold", 10),
        )
        self.style.map("Treeview", background=[("selected", "#dbeafe")], foreground=[("selected", "#111827")])
        self.style.map("Treeview.Heading", background=[("active", "#dce5f2")])

    def _apply_tree_stripes(self, tree: ttk.Treeview, rows: list[tuple]) -> None:
        tree.tag_configure("even", background=self.c_surface)
        tree.tag_configure("odd", background=self.c_row_alt)
        for i, values in enumerate(rows):
            tag = "even" if i % 2 == 0 else "odd"
            tree.insert("", tk.END, values=values, tags=(tag,))

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Port:", style="Section.TLabel").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=20, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(6, 10))
        self.port_combo.bind("<<ComboboxSelected>>", self._on_port_selected)
        self.port_combo.bind("<FocusIn>", self._on_port_focus_in)
        ttk.Button(top, text="Refresh Ports", command=self.refresh_ports, style="Accent.TButton").pack(side=tk.LEFT)

        ttk.Label(top, text="Baud:", style="Section.TLabel").pack(side=tk.LEFT, padx=(14, 0))
        ttk.Entry(top, textvariable=self.baud_var, width=10).pack(side=tk.LEFT, padx=(6, 10))

        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connection, style="Accent.TButton")
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, textvariable=self.status_var, style="Muted.TLabel").pack(side=tk.LEFT, padx=(8, 0))

        # Right side: RTC time and WiFi status
        right_frame = ttk.Frame(top)
        right_frame.pack(side=tk.RIGHT, padx=(8, 0))
        
        # WiFi status frame (on the left of right_frame)
        wifi_icon_font = (WIFI_FONT_NAME, 10) if WIFI_FONT_PATH else ("Segoe UI", 10)
        
        # Create a frame to hold WiFi icon and SSID
        wifi_frame = tk.Frame(right_frame, bg=self.c_bg)
        wifi_frame.pack(side=tk.LEFT)
        
        # RTC date/time label on far right (after WiFi)
        standard_font = ("Segoe UI", 9)
        self.rtc_time_label = tk.Label(right_frame, text="---- -- --:--", font=standard_font, bg=self.c_bg, fg=self.c_muted)
        self.rtc_time_label.pack(side=tk.LEFT, padx=(12, 0))
        
        # Name label on left uses standard font
        self.wifi_name_label = tk.Label(wifi_frame, text="", font=standard_font, bg=self.c_bg, fg=self.c_muted)
        self.wifi_name_label.pack(side=tk.LEFT)
        
        # Icon label on right uses WiFi font
        self.wifi_icon_label = tk.Label(wifi_frame, text="", font=wifi_icon_font, bg=self.c_bg, fg=self.c_muted)
        self.wifi_icon_label.pack(side=tk.LEFT, padx=(4, 0))
        
        # Keep reference for backward compatibility
        self.wifi_top_label = None

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.live_tab = ttk.Frame(notebook, padding=10)
        self.files_tab = ttk.Frame(notebook, padding=10)
        self.wifi_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.live_tab, text="Live Data")
        notebook.add(self.files_tab, text="Files")
        notebook.add(self.wifi_tab, text="Settings")

        self._build_live_tab()
        self._build_files_tab()
        self._build_wifi_tab()
        
        # Initialize WiFi status display
        self._update_wifi_top_bar()

    def _build_live_tab(self) -> None:
        btns = ttk.Frame(self.live_tab)
        btns.pack(fill=tk.X)
        self.live_start_btn = ttk.Button(
            btns,
            text="Start Live",
            command=self.start_live,
            state=tk.DISABLED,
            style="Accent.TButton",
        )
        self.live_start_btn.pack(side=tk.LEFT)
        self.live_stop_btn = ttk.Button(btns, text="Stop Live", command=self.stop_live, state=tk.DISABLED, style="Accent.TButton")
        self.live_stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.live_graphs_btn = ttk.Button(
            btns,
            text="Open Graphs",
            command=self.open_live_graphs,
            state=tk.DISABLED,
            style="Accent.TButton",
        )
        self.live_graphs_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = ttk.Panedwindow(self.live_tab, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=2)
        split.add(right, weight=1)

        ttk.Label(left, text="Incoming CSV Lines", style="Section.TLabel").pack(anchor="w")
        self.live_text = tk.Text(left, wrap="none", height=20)
        self.live_text.pack(fill=tk.BOTH, expand=True)
        self.live_text.configure(
            state=tk.DISABLED,
            bg=self.c_surface,
            fg=self.c_text,
            insertbackground=self.c_text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.c_border,
            padx=8,
            pady=6,
        )

        scroll_y = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.live_text.yview)
        self.live_text.configure(yscrollcommand=scroll_y.set)
        scroll_y.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        ttk.Label(right, text="Latest Row", style="Section.TLabel").pack(anchor="w")
        self.latest_tree = ttk.Treeview(right, columns=("field", "value"), show="headings", height=18)
        self.latest_tree.heading("field", text="Field")
        self.latest_tree.heading("value", text="Value")
        self.latest_tree.column("field", width=90, anchor="w")
        self.latest_tree.column("value", width=220, anchor="w")
        self.latest_tree.pack(fill=tk.BOTH, expand=True)

    def _build_files_tab(self) -> None:
        btns = ttk.Frame(self.files_tab)
        btns.pack(fill=tk.X)
        self.refresh_files_btn = ttk.Button(
            btns, text="Refresh File List", command=self.refresh_files, state=tk.DISABLED, style="Accent.TButton"
        )
        self.refresh_files_btn.pack(side=tk.LEFT)
        self.download_btn = ttk.Button(
            btns, text="Download Selected CSV", command=self.download_selected, state=tk.DISABLED, style="Accent.TButton"
        )
        self.download_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = ttk.Panedwindow(self.files_tab, orient=tk.VERTICAL)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        top = ttk.Frame(split)
        bottom = ttk.Frame(split)
        split.add(top, weight=2)
        split.add(bottom, weight=1)

        self.files_tree = ttk.Treeview(top, columns=("idx", "path", "size"), show="headings")
        self.files_tree.heading("idx", text="#")
        self.files_tree.heading("path", text="Path")
        self.files_tree.heading("size", text="Bytes")
        self.files_tree.column("idx", width=60, anchor="center")
        self.files_tree.column("path", width=650, anchor="w")
        self.files_tree.column("size", width=120, anchor="e")
        self.files_tree.pack(fill=tk.BOTH, expand=True)
        self.files_tree.bind("<<TreeviewSelect>>", self.on_file_selected)

        ttk.Label(bottom, textvariable=self.preview_title_var, style="Section.TLabel").pack(anchor="w")
        preview_wrap = ttk.Frame(bottom)
        preview_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        self.preview_tree = ttk.Treeview(preview_wrap, show="headings", height=12)
        self.preview_tree.grid(row=0, column=0, sticky="nsew")
        preview_wrap.rowconfigure(0, weight=1)
        preview_wrap.columnconfigure(0, weight=1)

        pv_y = ttk.Scrollbar(preview_wrap, orient=tk.VERTICAL, command=self.preview_tree.yview)
        pv_y.grid(row=0, column=1, sticky="ns")
        pv_x = ttk.Scrollbar(preview_wrap, orient=tk.HORIZONTAL, command=self.preview_tree.xview)
        pv_x.grid(row=1, column=0, sticky="ew")
        self.preview_tree.configure(yscrollcommand=pv_y.set, xscrollcommand=pv_x.set)

        ttk.Label(bottom, textvariable=self.preview_info_var, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_wifi_tab(self) -> None:
        btns = ttk.Frame(self.wifi_tab)
        btns.pack(fill=tk.X, pady=(0, 10))

        self.wifi_scan_btn = ttk.Button(
            btns,
            text="Scan Networks",
            command=self.wifi_scan,
            state=tk.DISABLED,
            style="Accent.TButton",
        )
        self.wifi_scan_btn.pack(side=tk.LEFT)

        self.wifi_saved_btn = ttk.Button(
            btns,
            text="Saved Networks",
            command=self._toggle_saved_networks_view,
            state=tk.DISABLED,
            style="Accent.TButton",
        )
        self.wifi_saved_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.wifi_reset_btn = ttk.Button(
            btns,
            text="Forget All",
            command=self.wifi_reset,
            state=tk.DISABLED,
            style="Accent.TButton",
        )
        self.wifi_reset_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = ttk.Panedwindow(self.wifi_tab, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=1)

        # Left side: Available Networks (combined list of saved + scanned + "Other")
        ttk.Label(left, text="Networks", style="Section.TLabel").pack(anchor="w")
        net_wrap = ttk.Frame(left)
        net_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        
        self.wifi_network_tree = ttk.Treeview(net_wrap, columns=("SSID", "RSSI", "Security"), show="headings", height=12)
        self.wifi_network_tree.heading("SSID", text="SSID")
        self.wifi_network_tree.heading("RSSI", text="Signal (dBm)")
        self.wifi_network_tree.heading("Security", text="Security")
        self.wifi_network_tree.column("SSID", width=180, anchor="w")
        self.wifi_network_tree.column("RSSI", width=100, anchor="center")
        self.wifi_network_tree.column("Security", width=120, anchor="w")
        self.wifi_network_tree.rowheight = 36
        self.wifi_network_tree.grid(row=0, column=0, sticky="nsew")
        self.wifi_network_tree.bind("<<TreeviewSelect>>", self._on_network_select)
        
        net_y = ttk.Scrollbar(net_wrap, orient=tk.VERTICAL, command=self.wifi_network_tree.yview)
        net_y.grid(row=0, column=1, sticky="ns")
        self.wifi_network_tree.configure(yscrollcommand=net_y.set)
        
        net_wrap.rowconfigure(0, weight=1)
        net_wrap.columnconfigure(0, weight=1)

        # Right side: Dynamic form area
        ttk.Label(right, text="Connect", style="Section.TLabel").pack(anchor="w", padx=(12, 0))
        self.wifi_form_frame = ttk.Frame(right)
        self.wifi_form_frame.pack(fill=tk.BOTH, expand=True, pady=(12, 0), padx=(12, 0))

    def _clear_wifi_form(self) -> None:
        """Clear all widgets from the form area."""
        for widget in self.wifi_form_frame.winfo_children():
            widget.destroy()
        self.wifi_current_form_type = None

    def _show_saved_network_form(self, ssid: str) -> None:
        """Show form for a saved network (Connect/Disconnect and Forget buttons)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "saved"
        self.wifi_current_form_ssid = ssid
        
        is_connected = self.wifi_current_ssid == ssid

        ttk.Label(self.wifi_form_frame, text=f"Saved Network: {ssid}", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        
        btn_frame = ttk.Frame(self.wifi_form_frame)
        btn_frame.pack(fill=tk.X)
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        
        if is_connected:
            ttk.Button(btn_frame, text="Disconnect", command=self._do_disconnect_from_form, style="Accent.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        else:
            ttk.Button(btn_frame, text="Connect", command=self._do_connect_saved, style="Accent.TButton").grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(btn_frame, text="Forget", command=self._do_forget_saved, style="Accent.TButton").grid(row=0, column=1, sticky="ew")

    def _show_psk_network_form(self, ssid: str, security: str) -> None:
        """Show form for a PSK network (password field and Connect/Disconnect button)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "psk"
        self.wifi_current_form_ssid = ssid
        
        is_connected = self.wifi_current_ssid == ssid

        ttk.Label(self.wifi_form_frame, text=f"Network: {ssid}", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(self.wifi_form_frame, text=f"Security: {security}", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        if not is_connected:
            ttk.Label(self.wifi_form_frame, text="Password:").pack(anchor="w")
            self.wifi_form_password = ttk.Entry(self.wifi_form_frame, show="*")
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 12))
            ttk.Button(self.wifi_form_frame, text="Connect", command=self._do_connect_psk, style="Accent.TButton").pack(fill=tk.X)
        else:
            ttk.Button(self.wifi_form_frame, text="Disconnect", command=self._do_disconnect_from_form, style="Accent.TButton").pack(fill=tk.X)

    def _show_ent_network_form(self, ssid: str, security: str) -> None:
        """Show form for an ENT network (username, password, and Connect/Disconnect button)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "ent"
        self.wifi_current_form_ssid = ssid
        
        is_connected = self.wifi_current_ssid == ssid

        ttk.Label(self.wifi_form_frame, text=f"Network: {ssid}", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(self.wifi_form_frame, text=f"Security: {security}", style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        if not is_connected:
            ttk.Label(self.wifi_form_frame, text="Username:").pack(anchor="w")
            self.wifi_form_username = ttk.Entry(self.wifi_form_frame)
            self.wifi_form_username.pack(fill=tk.X, pady=(4, 8))

            ttk.Label(self.wifi_form_frame, text="Password:").pack(anchor="w")
            self.wifi_form_password = ttk.Entry(self.wifi_form_frame, show="*")
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 12))
            ttk.Button(self.wifi_form_frame, text="Connect", command=self._do_connect_ent, style="Accent.TButton").pack(fill=tk.X)
        else:
            ttk.Button(self.wifi_form_frame, text="Disconnect", command=self._do_disconnect_from_form, style="Accent.TButton").pack(fill=tk.X)

    def _show_other_network_form(self) -> None:
        """Show form for 'Other' network (SSID, security type, and password/username fields)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "other"

        ttk.Label(self.wifi_form_frame, text="Manual Network Entry", style="Section.TLabel").pack(anchor="w", pady=(0, 10))

        ttk.Label(self.wifi_form_frame, text="Network Name (SSID):").pack(anchor="w")
        self.wifi_form_ssid = ttk.Entry(self.wifi_form_frame)
        self.wifi_form_ssid.pack(fill=tk.X, pady=(4, 8))

        ttk.Label(self.wifi_form_frame, text="Security Type:").pack(anchor="w")
        self.wifi_form_security = ttk.Combobox(
            self.wifi_form_frame,
            values=["None", "WPA2-PSK", "WPA2-ENT"],
            state="readonly"
        )
        self.wifi_form_security.pack(fill=tk.X, pady=(4, 8))
        self.wifi_form_security.bind("<<ComboboxSelected>>", self._on_other_security_changed)

        # Container for dynamic fields
        self.wifi_form_fields_frame = ttk.Frame(self.wifi_form_frame)
        self.wifi_form_fields_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        # Add Connect button at the bottom
        ttk.Button(self.wifi_form_frame, text="Connect", command=self._do_connect_other, style="Accent.TButton").pack(fill=tk.X)

    def _on_other_security_changed(self, _event=None) -> None:
        """Update form fields when security type changes in 'Other' form."""
        # Clear previous fields
        for widget in self.wifi_form_fields_frame.winfo_children():
            widget.destroy()

        security = self.wifi_form_security.get()

        if security == "None":
            # No fields needed
            pass
        elif security == "WPA2-PSK":
            ttk.Label(self.wifi_form_fields_frame, text="Password:").pack(anchor="w")
            self.wifi_form_password = ttk.Entry(self.wifi_form_fields_frame, show="*")
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 0))
        elif security == "WPA2-ENT":
            ttk.Label(self.wifi_form_fields_frame, text="Username:").pack(anchor="w")
            self.wifi_form_username = ttk.Entry(self.wifi_form_fields_frame)
            self.wifi_form_username.pack(fill=tk.X, pady=(4, 8))
            ttk.Label(self.wifi_form_fields_frame, text="Password:").pack(anchor="w")
            self.wifi_form_password = ttk.Entry(self.wifi_form_fields_frame, show="*")
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 0))

    def _on_network_select(self, _event=None) -> None:
        """Handle network selection and show appropriate form."""
        sel = self.wifi_network_tree.selection()
        if not sel:
            self._clear_wifi_form()
            return

        item = self.wifi_network_tree.item(sel[0])
        values = item["values"]
        
        if not values:
            return

        ssid = values[0]
        rssi = values[1] if len(values) > 1 else ""
        security = values[2] if len(values) > 2 else ""

        # Check if this is "Other"
        if ssid == "Other":
            self._show_other_network_form()
            return

        # Check if this is a saved network (has RSSI/Security but user meant it's saved)
        if ssid in self.wifi_saved_networks:
            self._show_saved_network_form(ssid)
            return

        # New network - determine form type by security
        if security == "OPEN" or security == "None":
            # For OPEN networks, still show a password field option
            self._show_psk_network_form(ssid, security)
        elif "ENT" in security.upper():
            self._show_ent_network_form(ssid, security)
        else:
            self._show_psk_network_form(ssid, security)

    def _update_files_controls(self) -> None:
        refresh_ok = self.connected and (not self.live_running) and (not self.files_refresh_inflight)
        self.refresh_files_btn.configure(state=tk.NORMAL if refresh_ok else tk.DISABLED)
        download_ok = refresh_ok and bool(self.file_entries)
        self.download_btn.configure(state=tk.NORMAL if download_ok else tk.DISABLED)

    def _schedule_poll(self) -> None:
        self.after(100, self._poll_events)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "error":
                    self.status_var.set(f"Error: {payload}")
                    messagebox.showerror("Error", str(payload))
                elif kind == "connected":
                    self.connected = True
                    self.connected_port = str(payload)
                    self.status_var.set(f"Connected: {payload}")
                    self.connect_btn.configure(text="Disconnect")
                    self._set_port_combo_state()
                    self.live_start_btn.configure(state=tk.NORMAL)
                    self.live_graphs_btn.configure(state=tk.NORMAL)
                    self._update_wifi_controls()
                    self.refresh_ports()
                    self._update_files_controls()
                    # Auto-load SD file list right after serial connection opens.
                    self.refresh_files()
                    # Auto-load saved WiFi networks
                    self.wifi_load_saved()
                    # Auto-scan WiFi networks on initial connection
                    self.wifi_scan()
                    # Start RTC polling
                    self._schedule_rtc_poll()
                elif kind == "disconnected":
                    self.connected = False
                    self.connected_port = None
                    self.live_running = False
                    self.files_refresh_inflight = False
                    self.preview_loading_index = None
                    self.status_var.set("Disconnected")
                    self.connect_btn.configure(text="Connect")
                    self._set_port_combo_state()
                    self.live_start_btn.configure(state=tk.DISABLED)
                    self.live_stop_btn.configure(state=tk.DISABLED)
                    self.live_graphs_btn.configure(state=tk.DISABLED)
                    self._update_wifi_controls()
                    self.refresh_ports()
                    self._update_files_controls()
                    self.file_entries = []
                    self.preview_cache.clear()
                    for row in self.files_tree.get_children():
                        self.files_tree.delete(row)
                    self._set_preview_message("", "Connect and refresh files to preview.", 0)
                    for row in self.wifi_network_tree.get_children():
                        self.wifi_network_tree.delete(row)
                    self.wifi_current_ssid = ""
                    self.wifi_current_rssi = ""
                    self._cancel_wifi_poll()
                    self._cancel_rtc_poll()
                    self._update_wifi_top_bar()
                elif kind == "live_line":
                    self._append_live_line(str(payload))
                elif kind == "files":
                    if self.connected:
                        self._show_files(payload)
                elif kind == "download_ok":
                    path, nbytes = payload
                    self.status_var.set(f"Downloaded: {path}")
                    messagebox.showinfo("Download Complete", f"Saved:\n{path}\n\n{nbytes} bytes")
                elif kind == "preview_ok":
                    if not self.connected:
                        continue
                    index, path, headers, rows, nbytes, info = payload
                    self.preview_cache[index] = (path, headers, rows, nbytes, info)
                    if self._current_selected_index() == index:
                        self._set_preview_table(path, headers, rows, nbytes, info)
                    self.status_var.set(f"Preview loaded: {path}")
                elif kind == "preview_done":
                    if self.preview_loading_index == payload:
                        self.preview_loading_index = None
                elif kind == "live_started":
                    self.live_running = True
                    self._reset_live_history()
                    self.status_var.set("Live stream running")
                    self.live_start_btn.configure(state=tk.DISABLED)
                    self.live_stop_btn.configure(state=tk.NORMAL)
                    self._update_wifi_controls()
                    self._update_files_controls()
                elif kind == "live_stopped":
                    self.live_running = False
                    self.status_var.set("Connected")
                    self.live_start_btn.configure(state=tk.NORMAL)
                    self.live_stop_btn.configure(state=tk.DISABLED)
                    self._update_wifi_controls()
                    self._update_files_controls()
                elif kind == "refresh_done":
                    self.files_refresh_inflight = False
                    self._update_files_controls()
                elif kind == "wifi_scan_ok":
                    self.wifi_scan_cache = payload
                    self._show_wifi_networks(payload)
                elif kind == "wifi_scan_done":
                    self.wifi_scan_inflight = False
                    self._update_wifi_controls()
                elif kind == "wifi_connect_done":
                    self.wifi_scan_btn.configure(state=tk.NORMAL if self.connected else tk.DISABLED)
                elif kind == "wifi_connect_ok":
                    result = payload
                    if result["success"]:
                        self.wifi_current_ssid = result['ssid']
                        # Fetch WiFi status immediately to get RSSI before starting poll
                        def fetch_status() -> None:
                            try:
                                status = self.client.wifi_get_status(timeout_s=5.0)
                                self.events.put(("wifi_status_ok", status))
                            except Exception:
                                pass
                        threading.Thread(target=fetch_status, daemon=True).start()
                        self._schedule_wifi_poll()
                        # Refresh saved networks after successful connection
                        self.wifi_load_saved()
                        # Refresh network list to update connection status
                        if not self.live_running:
                            self.wifi_scan()
                        # Refresh the form to show Disconnect button
                        if self.wifi_network_tree.selection():
                            self._on_network_select()
                    else:
                        pass
                elif kind == "wifi_disconnect_ok":
                    self.wifi_current_ssid = ""
                    self.wifi_current_rssi = ""
                    self._update_wifi_top_bar()
                    self._cancel_wifi_poll()
                    # Refresh network list to update connection status
                    if not self.live_running:
                        self.wifi_scan()
                    # Refresh the form to show Connect button
                    if self.wifi_network_tree.selection():
                        self._on_network_select()
                elif kind == "wifi_forget_ok":
                    result = payload
                    success = result.get("success", False)
                    ssid = result.get("ssid", "")
                    message = result.get("message", "")
                    
                    if success:
                        # Remove from saved networks list and refresh display
                        if ssid in self.wifi_saved_networks:
                            self.wifi_saved_networks.remove(ssid)
                        # Refresh the unified tree
                        if self.wifi_scan_cache:
                            self._show_wifi_networks(self.wifi_scan_cache)
                        self._clear_wifi_form()
                        messagebox.showinfo("Network Forgotten", f"Successfully forgot network '{ssid}'.")
                    else:
                        messagebox.showerror("Forget Failed", f"Failed to forget network '{ssid}': {message}")
                elif kind == "wifi_reset_ok":
                    self.wifi_saved_networks = []
                    # Refresh the unified tree
                    if self.wifi_scan_cache:
                        self._show_wifi_networks(self.wifi_scan_cache)
                elif kind == "wifi_saved_ok":
                    networks = payload
                    self.wifi_saved_networks = networks
                    # Refresh the unified tree with the updated saved networks list
                    if self.wifi_scan_cache:
                        self._show_wifi_networks(self.wifi_scan_cache)
                elif kind == "wifi_status_ok":
                    status = payload
                    # Always update SSID and RSSI from latest status, even if empty (means disconnected)
                    new_ssid = status.get("ssid", "")
                    new_rssi = status.get("rssi", "")
                    
                    # Update if values changed
                    ssid_changed = (self.wifi_current_ssid != new_ssid)
                    rssi_changed = (self.wifi_current_rssi != new_rssi)
                    
                    if ssid_changed or rssi_changed:
                        self.wifi_current_ssid = new_ssid
                        self.wifi_current_rssi = new_rssi
                        self._update_wifi_top_bar()
                elif kind == "rtc_time_ok":
                    result = payload
                    if result.get("success", False):
                        self.rtc_current_datetime = result.get("time", "---- -- --:--")
                        self.rtc_time_label.configure(text=self.rtc_current_datetime)
                elif kind == "busy":
                    self.status_var.set(str(payload))
        except Empty:
            pass
        self._schedule_poll()

    def refresh_ports(self) -> None:
        ports = [p.device for p in list_ports.comports()]
        if self.connected_port and self.connected_port not in ports:
            ports.append(self.connected_port)

        displays: list[str] = []
        mapping: dict[str, str] = {}
        for dev in ports:
            label = f"{dev} (connected)" if self.connected and self.connected_port == dev else dev
            displays.append(label)
            mapping[label] = dev

        self.port_display_to_device = mapping
        self.port_combo["values"] = displays

        current = self.port_var.get()
        if current in mapping:
            return
        if self.connected and self.connected_port:
            connected_label = f"{self.connected_port} (connected)"
            if connected_label in mapping:
                self.port_var.set(connected_label)
                return
        if displays:
            self.port_var.set(displays[0])

    def toggle_connection(self) -> None:
        if self.client.is_open:
            self.client.close()
            self.events.put(("disconnected", None))
            return

        selected = self.port_var.get().strip()
        port = self.port_display_to_device.get(selected, selected)
        if not port:
            messagebox.showwarning("Port Required", "Choose a COM port first.")
            return
        try:
            baud = int(self.baud_var.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid Baud", "Baud must be an integer.")
            return

        def worker() -> None:
            try:
                self.client.open(port, baud)
                self.events.put(("connected", port))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def start_live(self) -> None:
        def on_line(line: str) -> None:
            self.events.put(("live_line", line))

        def worker() -> None:
            try:
                self.events.put(("busy", "Starting live stream..."))
                self.client.start_live(on_line)
                self.events.put(("live_started", None))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def stop_live(self) -> None:
        def worker() -> None:
            try:
                self.client.stop_live()
                self.events.put(("live_stopped", None))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def refresh_files(self) -> None:
        if self.files_refresh_inflight:
            return
        self.files_refresh_inflight = True
        self._update_files_controls()

        def worker() -> None:
            try:
                self.events.put(("busy", "Loading file list..."))
                files = self.client.list_logs(timeout_s=20.0)
                self.events.put(("files", files))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("refresh_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _show_files(self, files: list[FileEntry]) -> None:
        self.file_entries = files
        self.preview_cache.clear()
        for row in self.files_tree.get_children():
            self.files_tree.delete(row)
        striped_rows = [(f.index, f.path, f.size) for f in files]
        self._apply_tree_stripes(self.files_tree, striped_rows)
        self._set_preview_message("", "Select a file to preview it.", 0)
        self._update_files_controls()
        if files:
            self.status_var.set(f"{len(files)} file(s) found")
        else:
            self.status_var.set("No log files found")

    def _current_selected_index(self) -> int | None:
        sel = self.files_tree.selection()
        if not sel:
            return None
        row = self.files_tree.item(sel[0], "values")
        if not row:
            return None
        return int(row[0])

    @staticmethod
    def _render_preview_table(
        payload: bytes,
        head_rows: int = 40,
        tail_rows: int = 40,
        max_cols: int = 18,
    ) -> tuple[list[str], list[list[str]], str]:
        text = payload.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        parsed = list(reader)
        if not parsed:
            return ["message"], [["(empty file)"]], ""

        width = max(len(r) for r in parsed)
        if width <= 0:
            return ["message"], [["(empty file)"]], ""

        header = parsed[0]
        body = parsed[1:]

        col_n = min(width, max_cols)
        headers: list[str] = []
        for i in range(col_n):
            if i < len(header) and header[i].strip():
                headers.append(header[i].strip())
            else:
                headers.append(f"col_{i}")

        rows: list[list[str]] = []
        total_rows = len(body)
        keep = head_rows + tail_rows
        show_split = total_rows > keep

        if show_split:
            front = body[:head_rows]
            back = body[-tail_rows:]
            omitted = total_rows - keep
            for raw in front:
                row = [raw[i] if i < len(raw) else "" for i in range(col_n)]
                rows.append(row)

            marker = [""] * col_n
            marker[0] = f"... {omitted} row(s) omitted ..."
            rows.append(marker)

            for raw in back:
                row = [raw[i] if i < len(raw) else "" for i in range(col_n)]
                rows.append(row)
        else:
            for raw in body:
                row = [raw[i] if i < len(raw) else "" for i in range(col_n)]
                rows.append(row)

        extra_cols = max(0, width - col_n)
        info_parts: list[str] = []
        if show_split:
            info_parts.append(f"showing first {head_rows} and last {tail_rows} data rows")
        if extra_cols:
            info_parts.append(f"showing first {col_n} columns")
        info = " | ".join(info_parts)
        return headers, rows, info

    def _set_preview_message(self, path: str, message: str, nbytes: int) -> None:
        if path:
            self.preview_title_var.set(f"Preview: {path} ({nbytes} bytes)")
        else:
            self.preview_title_var.set("Preview")
        self.preview_tree.delete(*self.preview_tree.get_children())
        self.preview_tree["columns"] = ("message",)
        self.preview_tree.heading("message", text="Preview")
        self.preview_tree.column("message", width=760, anchor="w", stretch=True)
        self.preview_tree.insert("", tk.END, values=(message,))
        self.preview_info_var.set("")

    def _set_preview_table(
        self,
        path: str,
        headers: list[str],
        rows: list[list[str]],
        nbytes: int,
        info: str,
    ) -> None:
        self.preview_title_var.set(f"Preview: {path} ({nbytes} bytes)")
        self.preview_tree.delete(*self.preview_tree.get_children())

        col_ids = tuple(f"c{i}" for i in range(len(headers)))
        self.preview_tree["columns"] = col_ids
        uniform_width = 120
        for i, name in enumerate(headers):
            cid = col_ids[i]
            self.preview_tree.heading(cid, text=name)
            self.preview_tree.column(cid, width=uniform_width, anchor="w", stretch=False)

        striped_rows = [tuple(row) for row in rows]
        self._apply_tree_stripes(self.preview_tree, striped_rows)

        self.preview_info_var.set(info)

    def on_file_selected(self, _event=None) -> None:
        if self.files_refresh_inflight:
            return
        idx = self._current_selected_index()
        if idx is None:
            return
        if self.preview_loading_index == idx:
            return
        if self.preview_loading_index is not None:
            return
        row = self.files_tree.item(self.files_tree.selection()[0], "values")
        dev_path = str(row[1])

        cached = self.preview_cache.get(idx)
        if cached is not None:
            path, headers, rows, nbytes, info = cached
            self._set_preview_table(path, headers, rows, nbytes, info)
            return

        self._set_preview_message(dev_path, "Loading preview...", 0)
        self.preview_loading_index = idx

        def worker() -> None:
            try:
                self.events.put(("busy", f"Loading preview for {dev_path} ..."))
                path, payload = self.client.download_log_bytes(index=idx, timeout_s=20.0)
                headers, rows, info = self._render_preview_table(
                    payload,
                    head_rows=40,
                    tail_rows=40,
                    max_cols=18,
                )
                self.events.put(("preview_ok", (idx, path, headers, rows, len(payload), info)))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("preview_done", idx))

        threading.Thread(target=worker, daemon=True).start()

    def download_selected(self) -> None:
        sel = self.files_tree.selection()
        if not sel:
            messagebox.showwarning("Select File", "Select a file to download.")
            return

        row = self.files_tree.item(sel[0], "values")
        index = int(row[0])
        dev_path = str(row[1])
        default_name = os.path.basename(dev_path) or f"log_{index}.csv"
        if not default_name.lower().endswith(".csv"):
            default_name += ".csv"

        out = filedialog.asksaveasfilename(
            title="Save CSV",
            defaultextension=".csv",
            initialfile=default_name,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not out:
            return
        out_path = Path(out)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Downloading {dev_path} ..."))
                nbytes = self.client.download_log(index=index, output_path=out_path, timeout_s=20.0)
                self.events.put(("download_ok", (str(out_path), nbytes)))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _graphs_window_alive(self) -> bool:
        return self.live_graphs_window is not None and self.live_graphs_window.winfo_exists()

    def open_live_graphs(self) -> None:
        if not self.connected:
            messagebox.showwarning("Not Connected", "Connect to a COM port first.")
            return

        # Opening graphs implies live plotting intent, so start streaming automatically.
        if not self.live_running:
            self.start_live()

        if not self._graphs_window_alive():
            self.live_graphs_window = LiveGraphsWindow(self)
        else:
            self.live_graphs_window.lift()
            self.live_graphs_window.focus_force()
        self._refresh_live_graphs()

    def _refresh_live_graphs(self) -> None:
        if not self._graphs_window_alive():
            return
        if len(self.live_headers) <= 1 and self.live_series:
            self.live_headers = ["timestamp"] + [f"col_{i}" for i in range(1, len(self.live_series) + 1)]
        self.live_graphs_window.render(
            headers=list(self.live_headers),
            x_values=list(self.live_x_values),
            time_labels=list(self.live_time_labels),
            series=[list(s) for s in self.live_series],
        )

    def _reset_live_history(self) -> None:
        self.live_sample_counter = 0
        self.live_headers = ["timestamp"]
        self.live_x_values.clear()
        self.live_time_labels.clear()
        self.live_series = []
        self._refresh_live_graphs()

    def _ensure_live_series_shape(self, n_vars: int) -> None:
        if n_vars <= 0:
            return
        if len(self.live_series) != n_vars:
            if len(self.live_headers) != (n_vars + 1):
                self.live_headers = ["timestamp"] + [f"col_{i}" for i in range(1, n_vars + 1)]
            self.live_series = [deque(maxlen=self.live_history_limit) for _ in range(n_vars)]
            self.live_x_values.clear()
            self.live_time_labels.clear()
            self.live_sample_counter = 0

    @staticmethod
    def _parse_float_or_none(raw: str) -> float | None:
        t = raw.strip()
        if not t or t.upper() == "NA":
            return None
        try:
            return float(t)
        except ValueError:
            return None

    @staticmethod
    def _timestamp_to_axis_value(ts: str, fallback: float) -> float:
        s = ts.strip()
        if not s:
            return fallback
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).timestamp()
            except ValueError:
                continue
        return fallback

    def _update_live_history(self, fields: list[str]) -> None:
        n_vars = max(0, len(fields) - 1)
        if n_vars <= 0:
            return
        self._ensure_live_series_shape(n_vars)

        self.live_sample_counter += 1
        ts_label = fields[0].strip()
        x_val = self._timestamp_to_axis_value(ts_label, float(self.live_sample_counter))
        self.live_time_labels.append(ts_label)
        self.live_x_values.append(x_val)

        for i in range(n_vars):
            raw = fields[i + 1] if i + 1 < len(fields) else ""
            self.live_series[i].append(self._parse_float_or_none(raw))

    def _append_live_line(self, line: str) -> None:
        self.live_text.configure(state=tk.NORMAL)
        self.live_text.insert(tk.END, line + "\n")
        # Keep tail reasonably small for long sessions.
        if float(self.live_text.index("end-1c").split(".")[0]) > 2000:
            self.live_text.delete("1.0", "500.0")
        self.live_text.see(tk.END)
        self.live_text.configure(state=tk.DISABLED)

        if line.startswith(LIVE_HEADER_PREFIX):
            header_csv = line[len(LIVE_HEADER_PREFIX):].strip()
            hdr = [h.strip() for h in header_csv.split(",") if h.strip()]
            if hdr:
                self.live_headers = hdr
            self._refresh_live_graphs()
            return

        fields = line.split(",")
        self.latest_fields = fields
        # Try to extract and update RTC time from first field (timestamp)
        if fields and len(fields) > 0:
            self._update_rtc_from_timestamp(fields[0])
        keys = self.live_headers
        if len(keys) != len(fields):
            keys = ["timestamp"] + [f"col_{i}" for i in range(1, len(fields))]

        for row in self.latest_tree.get_children():
            self.latest_tree.delete(row)
        latest_rows: list[tuple[str, str]] = []
        for i, value in enumerate(fields):
            key = keys[i] if i < len(keys) else f"col_{i}"
            latest_rows.append((key, value))
        self._apply_tree_stripes(self.latest_tree, latest_rows)

        self._update_live_history(fields)
        self._refresh_live_graphs()

    def wifi_scan(self) -> None:
        """Scan for available WiFi networks."""
        if self.wifi_scan_inflight:
            return
        self.wifi_scan_inflight = True
        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                networks = self.client.wifi_scan(timeout_s=15.0)
                self.events.put(("wifi_scan_ok", networks))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("wifi_scan_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def wifi_connect(self) -> None:
        """Connect to a WiFi network."""
        ssid = self.wifi_ssid_var.get().strip()
        pswd = self.wifi_pswd_var.get()
        auth = self.wifi_auth_var.get()

        if not ssid:
            messagebox.showwarning("SSID Required", "Enter a network name (SSID)")
            return

        if not pswd:
            messagebox.showwarning("Password Required", "Enter a password")
            return

        self.wifi_connect_btn.configure(state=tk.DISABLED)
        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Connecting to {ssid}..."))
                result = self.client.wifi_connect(ssid, pswd, auth, timeout_s=20.0)
                self.events.put(("wifi_connect_ok", result))
                # Clear password after successful/failed attempt
                self.wifi_pswd_var.set("")
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.wifi_connect_btn.configure(state=tk.NORMAL if self.connected else tk.DISABLED)
                self.wifi_scan_btn.configure(state=tk.NORMAL if self.connected else tk.DISABLED)

        threading.Thread(target=worker, daemon=True).start()

    def wifi_disconnect(self) -> None:
        """Disconnect from WiFi."""
        def worker() -> None:
            try:
                self.events.put(("busy", "Disconnecting WiFi..."))
                result = self.client.wifi_disconnect(timeout_s=10.0)
                self.events.put(("wifi_disconnect_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def wifi_reset(self) -> None:
        """Forget all saved WiFi networks."""
        if messagebox.askyesno("Reset WiFi", "Forget all saved WiFi networks?"):
            self.wifi_reset_btn.configure(state=tk.DISABLED)

            def worker() -> None:
                try:
                    self.events.put(("busy", "Resetting WiFi settings..."))
                    result = self.client.wifi_reset(timeout_s=10.0)
                    self.events.put(("wifi_reset_ok", result))
                except Exception as exc:
                    self.events.put(("error", exc))
                finally:
                    self.wifi_reset_btn.configure(state=tk.NORMAL if self.connected else tk.DISABLED)

            threading.Thread(target=worker, daemon=True).start()

    def wifi_load_saved(self) -> None:
        """Load saved WiFi networks from device."""
        if not self.connected:
            return

        def worker() -> None:
            try:
                networks = self.client.wifi_get_saved_networks(timeout_s=10.0)
                self.events.put(("wifi_saved_ok", networks))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_saved_network_select(self, event) -> None:
        """Handle click on saved network - populate SSID field in connect section."""
        sel = self.wifi_saved_tree.selection()
        if not sel:
            return
        
        row = self.wifi_saved_tree.item(sel[0], "values")
        ssid = row[0] if row else ""
        
        if not ssid:
            return
        
        # Populate SSID field in the connect section
        self.wifi_ssid_var.set(ssid)
        # Clear password field so user must enter it
        self.wifi_pswd_var.set("")

    def _update_wifi_controls(self) -> None:
        """Enable/disable WiFi buttons based on connection state and live streaming."""
        # Disable WiFi controls during live streaming to avoid interference
        wifi_ok = self.connected and not self.live_running
        self.wifi_scan_btn.configure(state=tk.NORMAL if wifi_ok and not self.wifi_scan_inflight else tk.DISABLED)
        self.wifi_saved_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)
        self.wifi_reset_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)

    def _update_wifi_top_bar(self) -> None:
        """Update WiFi status display in top bar with WiFi strength icons."""
        if hasattr(self, 'wifi_icon_label') and self.wifi_icon_label:
            if self.wifi_current_ssid:
                # WiFi strength indicators (Unicode from polybar-wifi-ramp-icons)
                # Characters from private use area that display as WiFi signal strength icons
                wifi_icons = [
                    "\ue0da",  # WiFi disabled/off (crossed out)
                    "\ue0d5",  # No signal
                    "\ue0d6",  # Weak
                    "\ue0d7",  # Fair
                    "\ue0d8",  # Good
                    "\ue0d9",  # Strong/Excellent
                ]
                
                # Check if we have RSSI value
                if self.wifi_current_rssi == "":
                    # No RSSI value, use the WiFi disabled icon
                    icon = wifi_icons[0]
                else:
                    try:
                        rssi_int = int(self.wifi_current_rssi) if self.wifi_current_rssi else -100
                    except (ValueError, TypeError):
                        rssi_int = -100
                    
                    if rssi_int >= -50:
                        icon = wifi_icons[5]  # Excellent
                    elif rssi_int >= -60:
                        icon = wifi_icons[4]  # Good
                    elif rssi_int >= -70:
                        icon = wifi_icons[3]  # Fair
                    elif rssi_int >= -80:
                        icon = wifi_icons[2]  # Weak
                    else:
                        icon = wifi_icons[1]  # No signal
                
                # Update name and icon separately
                self.wifi_name_label.configure(text=self.wifi_current_ssid)
                self.wifi_icon_label.configure(text=icon)
            else:
                self.wifi_name_label.configure(text="Not connected")
                # Show WiFi disabled icon when not connected
                self.wifi_icon_label.configure(text="\ue0da")

    def _show_wifi_networks(self, networks: list[dict]) -> None:
        """Display scanned WiFi networks in the list."""
        # Reset to scanned view when networks are updated
        self.wifi_viewing_saved = False
        self.wifi_saved_btn.configure(text="Saved Networks")
        
        for row in self.wifi_network_tree.get_children():
            self.wifi_network_tree.delete(row)

        # Add only scanned networks (not saved networks)
        for net in networks:
            ssid = net.get("ssid", "")
            rssi = net.get("rssi", "")
            security = net.get("security", "")
            self.wifi_network_tree.insert("", tk.END, values=(ssid, rssi, security))

        # Add "Other" option at the bottom
        # Create a tag for bold text if it doesn't exist
        try:
            self.wifi_network_tree.tag_configure('bold', font=('TkDefaultFont', 10, 'bold'))
        except:
            pass
        self.wifi_network_tree.insert("", tk.END, values=("Other", "", ""))

    def _toggle_saved_networks_view(self) -> None:
        """Toggle between viewing scanned networks and saved networks."""
        if self.wifi_viewing_saved:
            # Switch back to scanned networks
            self.wifi_viewing_saved = False
            self.wifi_saved_btn.configure(text="Saved Networks")
            if self.wifi_scan_cache:
                self._show_wifi_networks(self.wifi_scan_cache)
        else:
            # Switch to saved networks
            self.wifi_viewing_saved = True
            self.wifi_saved_btn.configure(text="All Networks")
            self._show_saved_networks_only()

    def _show_saved_networks_only(self) -> None:
        """Display only saved networks in the list."""
        for row in self.wifi_network_tree.get_children():
            self.wifi_network_tree.delete(row)

        # Add all saved networks
        for ssid in self.wifi_saved_networks:
            # Try to find RSSI from scan cache if available
            net_info = next((n for n in self.wifi_scan_cache if n.get("ssid") == ssid), {})
            rssi = net_info.get("rssi", "")
            security = net_info.get("security", "")
            self.wifi_network_tree.insert("", tk.END, values=(ssid, rssi, security))

    def _do_connect_saved(self) -> None:
        """Connect to a saved network."""
        ssid = self.wifi_current_form_ssid
        if not ssid:
            return

        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Connecting to saved network {ssid}..."))
                result = self.client.wifi_connect_saved(ssid, timeout_s=20.0)
                self.events.put(("wifi_connect_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("wifi_connect_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _do_forget_saved(self) -> None:
        """Forget a saved network."""
        ssid = self.wifi_current_form_ssid
        if not ssid:
            return

        if not messagebox.askyesno("Forget Network", f"Forget saved network '{ssid}'?"):
            return

        def worker() -> None:
            try:
                self.events.put(("busy", f"Forgetting network {ssid}..."))
                result = self.client.wifi_forget_by_ssid(ssid, timeout_s=10.0)
                self.events.put(("wifi_forget_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))

        threading.Thread(target=worker, daemon=True).start()

    def _do_disconnect_from_form(self) -> None:
        """Disconnect from WiFi network."""
        self.wifi_disconnect()

    def _do_connect_psk(self) -> None:
        """Connect to PSK network with password."""
        ssid = self.wifi_current_form_ssid
        password = self.wifi_form_password.get() if self.wifi_form_password else ""

        if not password:
            messagebox.showwarning("Password Required", "Enter a password to connect.")
            return

        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Connecting to {ssid}..."))
                result = self.client.wifi_connect(ssid, password, "PSK", timeout_s=20.0)
                self.events.put(("wifi_connect_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("wifi_connect_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _do_connect_ent(self) -> None:
        """Connect to ENT network with username and password."""
        ssid = self.wifi_current_form_ssid
        username = self.wifi_form_username.get() if self.wifi_form_username else ""
        password = self.wifi_form_password.get() if self.wifi_form_password else ""

        if not username or not password:
            messagebox.showwarning("Credentials Required", "Enter both username and password to connect.")
            return

        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Connecting to {ssid}..."))
                result = self.client.wifi_connect(ssid, password, "WPA2-EAP", username=username, timeout_s=20.0)
                self.events.put(("wifi_connect_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("wifi_connect_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _do_connect_other(self) -> None:
        """Connect to manually entered network."""
        ssid = self.wifi_form_ssid.get() if self.wifi_form_ssid else ""
        security = self.wifi_form_security.get() if self.wifi_form_security else "None"

        if not ssid:
            messagebox.showwarning("SSID Required", "Enter a network name (SSID).")
            return

        if security == "None":
            messagebox.showinfo("Open Network", "Connecting to open network. This feature may need device menu.")
            return

        password = self.wifi_form_password.get() if self.wifi_form_password else ""
        username = self.wifi_form_username.get() if self.wifi_form_username else ""

        if security == "WPA2-PSK" and not password:
            messagebox.showwarning("Password Required", "Enter a password.")
            return

        if security == "WPA2-ENT" and (not username or not password):
            messagebox.showwarning("Credentials Required", "Enter both username and password.")
            return

        self.wifi_scan_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                self.events.put(("busy", f"Connecting to {ssid}..."))
                auth_type = "PSK" if security == "WPA2-PSK" else "WPA2-EAP"
                if security == "WPA2-PSK":
                    result = self.client.wifi_connect(ssid, password, auth_type, timeout_s=20.0)
                else:  # WPA2-ENT
                    result = self.client.wifi_connect(ssid, password, auth_type, username=username, timeout_s=20.0)
                self.events.put(("wifi_connect_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("wifi_connect_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_wifi_poll(self) -> None:
        """Schedule periodic WiFi status polling every 5 seconds."""
        self._cancel_wifi_poll()  # Cancel any existing timer
        self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)

    def _cancel_wifi_poll(self) -> None:
        """Cancel the WiFi polling timer."""
        if self.wifi_poll_timer is not None:
            self.after_cancel(self.wifi_poll_timer)
            self.wifi_poll_timer = None

    def _wifi_poll_tick(self) -> None:
        """Called periodically to poll WiFi status."""
        if not self.connected or self.wifi_poll_inflight:
            # Reschedule for next check
            self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)
            return

        # Only poll if we're still connected
        if not self.wifi_current_ssid:
            self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)
            return

        self.wifi_poll_inflight = True

        def worker() -> None:
            try:
                status = self.client.wifi_get_status(timeout_s=5.0)
                self.events.put(("wifi_status_ok", status))
            except Exception:
                # Silently fail - don't disrupt the user
                pass
            finally:
                self.wifi_poll_inflight = False
                # Reschedule for next check regardless of success/failure
                self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_rtc_poll(self) -> None:
        """Schedule periodic RTC time polling every 10 seconds."""
        self._cancel_rtc_poll()  # Cancel any existing timer
        self.rtc_poll_timer = self.after(10000, self._rtc_poll_tick)

    def _cancel_rtc_poll(self) -> None:
        """Cancel the RTC polling timer."""
        if self.rtc_poll_timer is not None:
            self.after_cancel(self.rtc_poll_timer)
            self.rtc_poll_timer = None

    def _rtc_poll_tick(self) -> None:
        """Called periodically to poll RTC time."""
        if not self.connected or self.rtc_poll_inflight:
            # Reschedule for next check
            self.rtc_poll_timer = self.after(10000, self._rtc_poll_tick)
            return

        self.rtc_poll_inflight = True

        def worker() -> None:
            try:
                result = self.client.get_rtc_time(timeout_s=3.0)
                self.events.put(("rtc_time_ok", result))
            except Exception:
                # Silently fail - don't disrupt the user
                pass
            finally:
                self.rtc_poll_inflight = False
                # Reschedule for next check regardless of success/failure
                self.rtc_poll_timer = self.after(10000, self._rtc_poll_tick)

        threading.Thread(target=worker, daemon=True).start()

    def _update_rtc_from_timestamp(self, timestamp_str: str) -> None:
        """Extract date and time from timestamp string (format: "2026-04-17 10:04:16") and update RTC label."""
        try:
            # Timestamp format is expected to be "YYYY-MM-DD HH:MM:SS"
            parts = timestamp_str.strip().split()
            if len(parts) >= 2:
                date_str = parts[0]  # Get "YYYY-MM-DD"
                time_parts = parts[1].split(":")  # Split "HH:MM:SS"
                if len(time_parts) >= 2:
                    time_str = f"{time_parts[0]}:{time_parts[1]}"  # Get "HH:MM" without seconds
                    datetime_display = f"{date_str} {time_str}"
                    if datetime_display != self.rtc_current_datetime:
                        self.rtc_current_datetime = datetime_display
                        self.rtc_time_label.configure(text=datetime_display)
        except Exception:
            # Silently fail if timestamp parsing fails
            pass

    def on_close(self) -> None:
        try:
            self._cancel_wifi_poll()
            if self._graphs_window_alive():
                self.live_graphs_window.destroy()
            self.client.close()
        finally:
            self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
