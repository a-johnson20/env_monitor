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
- customtkinter
"""

from __future__ import annotations

import csv
import ctypes
import io
import math
import os
import struct
import sys
import threading
import re
import time
import json
import tkinter as tk
from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageTk

import customtkinter as ctk
import serial
from serial.tools import list_ports

try:
    import paho.mqtt.client as mqtt
    HAS_PAHO_MQTT = True
except Exception:
    mqtt = None
    HAS_PAHO_MQTT = False

# ===========================================================================
# GUI VERSION  —  bump this on every GUI change
# ===========================================================================
# The firmware carries its own version (FIRMWARE_VERSION in src/app/main.cpp),
# stored on the ESP32 in NVS and reported over the serial protocol
# (ProtoCmd.FW_VERSION), so the two can be compared on every connection.
#
# Compatibility follows SemVer:
#   MAJOR — breaking change (serial protocol altered). Mismatched majors must be
#           re-paired, so the check reports incompatible.
#   MINOR — additive change (new command/response, extra field). Both revisions
#           still interoperate, but the newer side may have features the older
#           one cannot use, so the check warns rather than fails.
#   PATCH — fixes only, wire format identical: always interchangeable, ignored.
#
# The About line reports the comparison as one of four fixed verdicts (see
# version_compat below), where "protocol N" is the MAJOR number:
#   Compatible (protocol 1)
#   Compatible (protocol 1, minor version differs)
#   Incompatible (GUI protocol 2, firmware protocol 1)
#   Compatibility unknown (firmware protocol unknown)
GUI_VERSION = "1.4.3"

# Version-check levels — drive the colour of the Settings > About status line.
VERSION_LEVEL_OK = "ok"            # green — exact protocol revision match
VERSION_LEVEL_WARN = "warn"        # amber — compatible, but one side is newer
VERSION_LEVEL_ERROR = "error"      # red   — breaking change, re-pair required
VERSION_LEVEL_UNKNOWN = "unknown"  # grey  — version could not be read

VERSION_LEVEL_COLOURS = {
    VERSION_LEVEL_OK: "#16a34a",
    VERSION_LEVEL_WARN: "#d97706",
    VERSION_LEVEL_ERROR: "#dc2626",
    VERSION_LEVEL_UNKNOWN: "#888888",
}

# The one verdict used when no comparison is possible. Shared by version_compat()
# and the About read-failure path so the two can never drift apart.
VERSION_UNKNOWN_TEXT = "Compatibility unknown (firmware protocol unknown)"


# Pump control timing.
#   SEND_DEBOUNCE_MS — a slider move is forwarded after this pause, so a drag
#                      sends a handful of updates instead of one per pixel.
PUMP_SEND_DEBOUNCE_MS = 250


# CustomTkinter look & feel. The app is light-only: the graph canvases and
# Treeview tables draw with the fixed palette below (CustomTkinter has no
# canvas or table widget of its own).
# CustomTkinter enables per-monitor DPI awareness and rescales its own widgets
# when the window moves between monitors, so no manual DPI handling is needed.
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

C_BG = "#eef2f8"
C_SURFACE = "#ffffff"
C_TEXT = "#1f2937"
C_MUTED = "#5b6678"
C_ACCENT = "#0b66d6"
C_ACCENT_HOVER = "#0a58bc"
C_ACCENT_DISABLED = "#a8b5cc"
C_BORDER = "#d4dbe6"
C_ROW_ALT = "#f7f9fc"
C_TAB_IDLE = "#e3e9f3"
C_HINT = "#888888"


def ui_font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    """Segoe UI font; ``size`` is in pixels (13 px ≈ the old 10 pt)."""
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


class AccentButton(ctk.CTkButton):
    """Accent-coloured button that greys out while disabled."""

    def __init__(self, master, **kwargs) -> None:
        kwargs.setdefault("font", ui_font(13, "bold"))
        kwargs.setdefault("fg_color", C_ACCENT)
        kwargs.setdefault("hover_color", C_ACCENT_HOVER)
        kwargs.setdefault("text_color_disabled", "#ecf2ff")
        kwargs.setdefault("height", 32)
        if kwargs.get("state") == tk.DISABLED:
            kwargs["fg_color"] = C_ACCENT_DISABLED
        super().__init__(master, **kwargs)

    def configure(self, require_redraw=False, **kwargs):
        if "state" in kwargs:
            kwargs["fg_color"] = C_ACCENT if kwargs["state"] == tk.NORMAL else C_ACCENT_DISABLED
        return super().configure(require_redraw=require_redraw, **kwargs)


class Section(ctk.CTkFrame):
    """Bordered group with a title; children are packed straight into it."""

    def __init__(self, master, title: str) -> None:
        super().__init__(master, fg_color=C_SURFACE, border_width=1, border_color=C_BORDER, corner_radius=8)
        ctk.CTkLabel(self, text=title, font=ui_font(13, "bold"), text_color=C_TEXT).pack(
            anchor="w", padx=10, pady=(6, 0))


def make_frame(master, **kwargs) -> ctk.CTkFrame:
    """Transparent layout frame that shows whatever is behind it."""
    return ctk.CTkFrame(master, fg_color="transparent", **kwargs)


def make_label(master, text: str = "", *, hint: bool = False, **kwargs) -> ctk.CTkLabel:
    kwargs.setdefault("font", ui_font(13))
    kwargs.setdefault("text_color", C_HINT if hint else C_TEXT)
    return ctk.CTkLabel(master, text=text, **kwargs)


def make_readonly_entry(master, textvariable: tk.StringVar, width: int = 200) -> ctk.CTkEntry:
    """Selectable, copyable but not editable entry."""
    return ctk.CTkEntry(master, textvariable=textvariable, state="readonly", width=width, font=ui_font(13))

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

def get_icon_path() -> Path | None:
    """Find the app icon, checking bundled and development locations."""
    if getattr(sys, 'frozen', False):
        icon_path = Path(sys._MEIPASS) / "assets" / "GEM_icon_256.ico"
        if icon_path.exists():
            return icon_path

    repo_icon = Path(__file__).parent.parent / "assets" / "GEM_icon_256.ico"
    if repo_icon.exists():
        return repo_icon

    return None

APP_ICON_PATH = get_icon_path()


def parse_version(text: str):
    """Parse "MAJOR.MINOR[.PATCH]" into a (major, minor, patch) tuple.

    Missing/extra parts are tolerated (patch defaults to 0). Returns None when
    the string is not a version at all (e.g. the device did not answer).
    """
    try:
        parts = [int(p) for p in str(text).strip().split(".")[:3]]
    except (TypeError, ValueError):
        return None
    if len(parts) < 2:
        return None
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def version_compat(gui_version: str, fw_version: str):
    """Compare GUI and firmware versions using SemVer rules.

    Returns (compatible, status_text, level). `compatible` says whether the pair
    can be used together at all; `level` refines that for the indicator colour
    ("ok" / "warn" / "error" / "unknown").

    "protocol N" is the MAJOR number — the serial-protocol revision. MINOR is an
    additive change (both revisions still interoperate) and PATCH is ignored, so
    the verdict text is always one of exactly four forms:

        Compatible (protocol 1)
        Compatible (protocol 1, minor version differs)
        Incompatible (GUI protocol 2, firmware protocol 1)
        Compatibility unknown (firmware protocol unknown)

    The full version strings stay visible in the About fields; only the verdict
    is condensed here. `level` is "ok" (exact protocol revision match), "warn"
    (compatible, minor differs), "error" (major differs — re-pair) or "unknown"
    (version unreadable).
    """
    fw = parse_version(fw_version)
    gui = parse_version(gui_version)
    # No verdict is possible unless both sides parse. The raw device reply is
    # still shown verbatim in the About "Firmware version" field.
    if fw is None or gui is None:
        return False, VERSION_UNKNOWN_TEXT, VERSION_LEVEL_UNKNOWN
    if fw[0] != gui[0]:
        return (False,
                f"Incompatible (GUI protocol {gui[0]}, firmware protocol {fw[0]})",
                VERSION_LEVEL_ERROR)
    if fw[1] != gui[1]:
        # Same major, different minor: additive-only change per SemVer, so both
        # revisions still interoperate — the newer side just may add features.
        return (True, f"Compatible (protocol {fw[0]}, minor version differs)",
                VERSION_LEVEL_WARN)
    return True, f"Compatible (protocol {fw[0]})", VERSION_LEVEL_OK


# Try to register the WiFi font on Windows
if WIFI_FONT_PATH and hasattr(ctypes, 'windll'):
    try:
        # Add font to registry for current session
        ctypes.windll.gdi32.AddFontResourceExW(str(WIFI_FONT_PATH), 0x10, 0)
    except Exception:
        pass  # Font loading failed, will fall back to default


# Binary Protocol Commands
class ProtoCmd:
    MAIN_MENU = 0x10
    LIVE_START = 0x11
    LIVE_STOP = 0x12
    WIFI_MENU = 0x13
    WIFI_SCAN = 0x14
    WIFI_CONNECT = 0x15
    WIFI_CONNECT_EAP = 0x16
    WIFI_SAVED_LIST = 0x17
    WIFI_DISCONNECT = 0x18
    WIFI_FORGET = 0x19
    WIFI_STATUS = 0x1A
    WIFI_CONNECT_SAVED = 0x20
    PUMP_SET = 0x21
    PUMP_GET = 0x22
    CALIB_MULTIPOINT = 0x23  # [ch: u8][r2ppm: 4-byte LE float][alpha: 4-byte LE float]
    LORA_INFO = 0x24
    LORA_GEN_KEYS = 0x25
    EEPROM_WRITE = 0x26  # [ch: u8][tgs_id: u16 BE][lot_len: u8][lot][board_len: u8][board]
    EEPROM_READ = 0x27   # payload [ch: u8]; reply "id,crc,lot,board" CSV
    EEPROM_CLEAR = 0x28  # payload [ch: u8]; erase ID/lot/board blocks back to blank
    FW_VERSION = 0x29    # no payload; reply FW_VERSION string
    CALIB_TIMESTAMP = 0x2A  # [ch: u8][unix time: 4-byte LE u32]; stores calib date in sensor EEPROM
    BOARD_ID_WRITE = 0x2B  # [len u8][ascii board id]; stores mainboard ID in ESP32 NVS
    BOARD_ID_READ = 0x2C   # no payload; reply BOARD_ID (0x26) length-prefixed string
    BOARD_ID_CLEAR = 0x2D  # no payload; erases the mainboard ID from ESP32 NVS
    LOG_MENU = 0x1B
    LOG_LIST = 0x1C
    LOG_GET = 0x1D
    RTC_TIME = 0x1E
    BACK = 0x1F

# Binary Protocol Response Types
class ProtoResp:
    OK = 0x00
    ERROR = 0x01
    STATUS = 0x02
    WIFI_LIST = 0x10
    WIFI_INFO = 0x11
    WIFI_SAVED = 0x12
    LOG_LIST = 0x13
    LOG_BEGIN = 0x14
    LOG_DATA = 0x15
    LOG_END = 0x16
    LIVE_DATA = 0xFE  # 0xFE avoids collision with ASCII space (0x20) in timestamps
    RTC_RESPONSE = 0x21
    PUMP_STATUS = 0x22
    LORA_INFO = 0x23   # DevEUI,AppEUI,AppKey CSV string
    EEPROM_IDS = 0x24  # TGS EEPROM IDs: "id,crc,lot,board" CSV string
    FW_VERSION = 0x25  # Firmware version string (MAJOR.MINOR.PATCH)
    BOARD_ID = 0x26    # Mainboard ID (length-prefixed string; empty = not set)
    PROMPT_STRING = 0x30
    PROMPT_CONFIRM = 0x31



@dataclass
class FileEntry:
    index: int
    path: str
    size: int


class SerialMenuClient:
    """Binary protocol serial client"""

    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.live_thread: threading.Thread | None = None
        self.live_stop = threading.Event()

    @property
    def is_open(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self, port: str, baud: int) -> None:
        """Open serial port without resetting the device (no DTR/RTS toggle)."""
        if self.is_open:
            self.close()
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = baud
        self.ser.timeout = 0.1
        self.ser.write_timeout = 2
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        """Close serial port"""
        self.stop_live()
        with self.lock:
            if self.ser is not None:
                try:
                    self.ser.close()
                finally:
                    self.ser = None

    def _require_open(self) -> serial.Serial:
        """Check that port is open"""
        if self.ser is None or not self.ser.is_open:
            raise RuntimeError("Serial port not open")
        return self.ser

    # ============ BINARY PROTOCOL HELPERS ============

    def _send_cmd(self, cmd: int) -> None:
        """Send single byte command"""
        ser = self._require_open()
        ser.write(bytes([cmd]))
        ser.flush()

    def _send_cmd_with_string(self, cmd: int, value: str) -> None:
        """Send command + length-prefixed string"""
        ser = self._require_open()
        ser.write(bytes([cmd]))
        encoded = value.encode('utf-8')
        if len(encoded) > 255:
            encoded = encoded[:255]
        ser.write(bytes([len(encoded)]))
        ser.write(encoded)
        ser.flush()

    def _send_cmd_with_byte(self, cmd: int, value: int) -> None:
        """Send command + single byte"""
        ser = self._require_open()
        ser.write(bytes([cmd, value & 0xFF]))
        ser.flush()

    def _read_exact(self, n: int, timeout_s: float = 2.0) -> bytes:
        """Read exactly n bytes"""
        ser = self._require_open()
        deadline = time.time() + timeout_s
        buf = bytearray()
        while len(buf) < n and time.time() < deadline:
            chunk = ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.001)
        if len(buf) != n:
            raise RuntimeError(f"Timeout reading {n} bytes, got {len(buf)}")
        return bytes(buf)

    def _read_byte(self, timeout_s: float = 2.0) -> int:
        """Read single byte"""
        return self._read_exact(1, timeout_s)[0]

    def _read_string(self, timeout_s: float = 2.0) -> str:
        """Read length-prefixed string"""
        length = self._read_byte(timeout_s)
        if length == 0:
            return ""
        data = self._read_exact(length, timeout_s)
        return data.decode('utf-8', errors='replace')

    # Device error codes (mirrors ui::proto::ErrorCode in the firmware)
    ERR_SD = 0x02

    @staticmethod
    def _describe_device_error(code: int) -> str:
        """Turn a device error code into a human-readable message."""
        names = {
            0x01: "invalid command",
            0x02: "SD card error (card missing, unreadable or mid-remount)",
            0x03: "WiFi error",
            0x04: "timeout",
            0x05: "invalid index",
            0x06: "no networks found",
            0x07: "not connected",
            0x08: "file not found",
        }
        if code in names:
            return f"Device error {code} ({names[code]})"
        return f"Device error {code}"

    def _drain_and_read_response(self, valid_types: set[int], timeout_s: float = 5.0) -> int:
        """Drain stale frames and read the first valid response type.

        After reset_input_buffer(), bytes that were already in transit over USB
        (e.g. LIVE_DATA frames from commit_and_reset_all_windows()) can still arrive.
        Known frames are consumed whole — dropping them byte-by-byte would let a
        stale reply's payload (e.g. a LOG_LIST file count) be misread as the start
        of the next response.

        Returns the response type byte.
        Raises RuntimeError on timeout.
        """
        ser = self._require_open()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            b = ser.read(1)
            if not b:
                time.sleep(0.001)
                continue
            resp = b[0]
            if resp in valid_types:
                return resp
            # Stale frame from a previous operation: consume it whole.
            try:
                self._consume_frame(resp, max(0.05, deadline - time.time()))
            except RuntimeError:
                if deadline - time.time() <= 0:
                    break
        raise RuntimeError(f"Timeout waiting for valid response (expected one of {valid_types})")

    # Frame types that carry a length-prefixed payload ([type][len][payload...]):
    # STATUS(0x02), LIVE_DATA(0xFE), RTC_RESPONSE(0x21), PUMP_STATUS(0x22),
    # LORA_INFO(0x23), EEPROM_IDS(0x24), FW_VERSION(0x25).
    _LEN_FRAMED = frozenset({
        ProtoResp.STATUS, ProtoResp.LIVE_DATA, ProtoResp.RTC_RESPONSE,
        ProtoResp.LORA_INFO, ProtoResp.PUMP_STATUS, ProtoResp.EEPROM_IDS,
        ProtoResp.FW_VERSION, ProtoResp.BOARD_ID,
    })

    def _consume_frame(self, resp: int, timeout_s: float) -> None:
        """Consume the remainder of a frame whose type byte `resp` was already read.

        Keeps the byte stream in sync so payload bytes of unsolicited frames
        can never be misread as the start of the next response.
        """
        if resp in self._LEN_FRAMED:
            self._read_string(timeout_s)                 # [type][len][payload]
        elif resp == ProtoResp.ERROR:
            self._read_byte(timeout_s)                   # [type][error_code]
        elif resp in (ProtoResp.WIFI_LIST, ProtoResp.WIFI_SAVED):
            count = self._read_byte(timeout_s)
            for _ in range(count):
                self._read_byte(timeout_s)               # rssi
                self._read_byte(timeout_s)               # security
                ssid_len = self._read_byte(timeout_s)
                if ssid_len:
                    self._read_exact(ssid_len, timeout_s)
        elif resp in (ProtoResp.LOG_BEGIN, ProtoResp.LOG_END):
            self._read_exact(4, timeout_s)               # size (u32 LE)
            path_len = self._read_byte(timeout_s)
            if path_len:
                self._read_exact(path_len, timeout_s)
        elif resp == ProtoResp.LOG_DATA:
            self._read_string(timeout_s)                 # [type][len][data]
        elif resp == ProtoResp.LOG_LIST:
            count = self._read_byte(timeout_s)
            for _ in range(count):
                self._read_exact(4, timeout_s)           # size (u32 LE)
                path_len = self._read_byte(timeout_s)
                if path_len:
                    self._read_exact(path_len, timeout_s)
        elif resp == ProtoResp.WIFI_INFO:
            connected = self._read_byte(timeout_s)
            if connected:
                self._read_byte(timeout_s)               # rssi
                for _ in range(4):                       # ssid, ip, gw, dns
                    self._read_string(timeout_s)
        # Anything else (PROMPT_*, stray bytes): no known payload to consume.

    def _read_response(self, expected, timeout_s: float = 5.0):
        """Frame-aware response reader.

        Reads from the serial stream until a frame whose type byte is in
        `expected` arrives. Frames the device may emit unsolicited (STATUS
        messages, live-data lines, RTC/LoRa replies, ...) are consumed so they
        can never be mistaken for the command's response — previously a stray
        byte (e.g. 91 = '[' from a "[...]" status text) surfaced as an
        "Unexpected response: N" popup.

        `expected` is an int or a set of response type bytes.
        Returns (resp_type, last_status_text_or_None).
        Raises RuntimeError on timeout.
        """
        if isinstance(expected, int):
            expected = {expected}
        deadline = time.time() + timeout_s
        last_status = None
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise RuntimeError(
                    f"Timeout waiting for response (expected one of {sorted(expected)})")
            resp = self._read_byte(remaining)
            if resp in expected:
                return resp, last_status
            if resp == ProtoResp.STATUS:
                # Keep the newest status text — useful for error messages.
                try:
                    last_status = self._read_string(max(0.05, deadline - time.time()))
                except RuntimeError:
                    if deadline - time.time() <= 0:
                        raise
                continue
            # Skip/consume any other unexpected frame so its payload cannot
            # be misparsed as the next response.
            try:
                self._consume_frame(resp, max(0.05, deadline - time.time()))
            except RuntimeError:
                if deadline - time.time() <= 0:
                    raise

    # ============ COMMAND METHODS ============

    def list_logs(self, timeout_s: float = 20.0) -> list[FileEntry]:
        """List log files"""
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before listing files")
        
        with self.lock:
            ser = self._require_open()
            resp_type = None
            # A single SD_ERROR is usually a transient hiccup or a card the
            # firmware is in the middle of remounting, so ask once more before
            # reporting failure.
            for attempt in (1, 2):
                ser.reset_input_buffer()
                self._send_cmd(ProtoCmd.LOG_LIST)

                # Read response — drain stale frames (e.g. in-transit LIVE_DATA)
                # until we see LOG_LIST, ERROR, or timeout.
                valid = {ProtoResp.LOG_LIST, ProtoResp.ERROR}
                resp_type = self._drain_and_read_response(valid, timeout_s)

                if resp_type != ProtoResp.ERROR:
                    break
                error_code = self._read_byte(timeout_s)
                if error_code != self.ERR_SD or attempt == 2:
                    raise RuntimeError(self._describe_device_error(error_code))
                time.sleep(0.6)

            # resp_type is guaranteed to be LOG_LIST here
            
            # Read file list
            num_files = self._read_byte(timeout_s)
            files = []
            
            for i in range(num_files):
                # Read: size (4 bytes LE) + path_len (1 byte) + path
                size_bytes = self._read_exact(4, timeout_s)
                size = int.from_bytes(size_bytes, 'little')
                
                path_len = self._read_byte(timeout_s)
                path = self._read_exact(path_len, timeout_s).decode('utf-8', errors='replace')
                
                files.append(FileEntry(index=i, path=path, size=size))
            
            return files

    def download_log_bytes(self, index: int, timeout_s: float = 20.0) -> tuple[str, bytes]:
        """Download log file"""
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before downloading")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            self._send_cmd_with_byte(ProtoCmd.LOG_GET, index)
            
            # Read response — drain stale bytes (e.g. in-transit LIVE_DATA frames)
            # until we see LOG_BEGIN, ERROR, or timeout.
            valid = {ProtoResp.LOG_BEGIN, ProtoResp.ERROR}
            resp_type = self._drain_and_read_response(valid, timeout_s)
            
            if resp_type == ProtoResp.ERROR:
                error_code = self._read_byte(timeout_s)
                raise RuntimeError(self._describe_device_error(error_code))
            
            # resp_type is guaranteed to be LOG_BEGIN here
            
            # Read file entry header
            size_bytes = self._read_exact(4, timeout_s)
            file_size = int.from_bytes(size_bytes, 'little')
            
            path_len = self._read_byte(timeout_s)
            path = self._read_exact(path_len, timeout_s).decode('utf-8', errors='replace')
            
            # Read file data
            payload = bytearray()
            remaining = file_size
            
            while remaining > 0:
                resp_type = self._read_byte(timeout_s)
                
                if resp_type == ProtoResp.LOG_DATA:
                    chunk_len = self._read_byte(timeout_s)
                    chunk = self._read_exact(chunk_len, timeout_s)
                    payload.extend(chunk)
                    remaining -= chunk_len
                elif resp_type == ProtoResp.LOG_END:
                    # Read end marker
                    self._read_exact(4, timeout_s)  # size
                    path_len = self._read_byte(timeout_s)
                    self._read_exact(path_len, timeout_s)  # path
                    break
                else:
                    raise RuntimeError(f"Unexpected response during transfer: {resp_type}")
            
            if len(payload) != file_size:
                raise RuntimeError(f"Size mismatch: {len(payload)} vs {file_size}")
            
            return path, bytes(payload)

    def download_log(self, index: int, output_path: Path, timeout_s: float = 20.0) -> int:
        """Download log file and save"""
        _, payload = self.download_log_bytes(index, timeout_s)
        output_path.write_bytes(payload)
        return len(payload)

    def start_live(self, on_line, on_status=None) -> None:
        """Start live streaming"""
        if self.live_thread is not None and self.live_thread.is_alive():
            return
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.LIVE_START)
            # Read response, draining any stale bytes that arrived between
            # the buffer reset and the OK response
            deadline = time.time() + 2.0
            got_ok = False
            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                if b[0] == ProtoResp.OK:
                    got_ok = True
                    break
            # Do NOT reset_input_buffer here — firmware sends the live header
            # immediately after the OK, flushing would discard it.
        
        self.live_stop.clear()
        self.live_thread = threading.Thread(
            target=self._live_loop,
            args=(on_line, on_status),
            daemon=True,
        )
        self.live_thread.start()

    def _live_loop(self, on_line, on_status=None) -> None:
        """Live stream loop - reads typed messages from device
        
        Format: [type byte][length byte][message data]
        Types: 0x02 = STATUS (debug), 0xFE = LIVE_DATA (CSV)
        CSV and STATUS messages are passed to their respective callbacks.
        """
        ser = self._require_open()
        
        while not self.live_stop.is_set():
            try:
                # Read message type
                b = ser.read(1)
                if not b:
                    continue
                
                msg_type = b[0]
                if msg_type not in (0x02, 0xFE):
                    continue  # not a frame start; skip a byte to resync

                # Length byte: wait for it rather than abandoning the frame
                length = self._read_exact(1, timeout_s=2.0)[0]
                if length == 0:
                    continue  # Skip zero-length messages
                
                # Read message data
                msg_data = self._read_exact(length, timeout_s=5.0)
                line = msg_data.decode("utf-8", errors="replace")
                
                # Only process LIVE_DATA (0xFE) messages
                if msg_type == 0xFE:
                    if line:
                        on_line(line)
                elif msg_type == ProtoResp.STATUS and on_status is not None:
                    on_status(line)
            except Exception:
                # Timeout mid-frame; keep buffered bytes and resync on a frame type
                time.sleep(0.01)
        try:
            self._send_cmd(ProtoCmd.LIVE_STOP)
        except:
            pass

    def stop_live(self) -> None:
        """Stop live streaming"""
        if self.live_thread is None:
            return
        self.live_stop.set()
        self.live_thread.join(timeout=2.0)
        self.live_thread = None

    def wifi_scan(self, timeout_s: float = 15.0) -> list[dict]:
        """Scan WiFi networks"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before scanning WiFi")

        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            self._send_cmd(ProtoCmd.WIFI_SCAN)
            
            # Read response — skip any unsolicited frames (STATUS text, RTC
            # replies, ...) so they can't be mistaken for the scan result.
            resp_type, _ = self._read_response(
                {ProtoResp.WIFI_LIST, ProtoResp.ERROR}, timeout_s)
            
            if resp_type == ProtoResp.ERROR:
                return []
            
            # Read network list
            num_networks = self._read_byte(timeout_s)
            networks = []
            
            for _ in range(num_networks):
                rssi_byte = self._read_byte(timeout_s)
                rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
                
                security = self._read_byte(timeout_s)
                ssid_len = self._read_byte(timeout_s)
                ssid = self._read_exact(ssid_len, timeout_s).decode('utf-8', errors='replace')
                
                sec_map = {0: "Open", 1: "WPA2-PSK", 2: "WPA2-Enterprise"}
                networks.append({
                    "ssid": ssid,
                    "rssi": str(rssi),
                    "security": sec_map.get(security, "Unknown")
                })
            
            return networks

    def wifi_connect(self, ssid: str, password: str, auth_type: str = "PSK", username: str = "", timeout_s: float = 60.0) -> dict:
        """Connect to WiFi"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                if auth_type == "WPA2-EAP":
                    self._send_cmd(ProtoCmd.WIFI_CONNECT_EAP)
                    # Send SSID, username, password
                    for s in [ssid, username, password]:
                        encoded = s.encode('utf-8')
                        if len(encoded) > 255:
                            encoded = encoded[:255]
                        ser.write(bytes([len(encoded)]))
                        ser.write(encoded)
                else:
                    self._send_cmd(ProtoCmd.WIFI_CONNECT)
                    # Send SSID, password
                    for s in [ssid, password]:
                        encoded = s.encode('utf-8')
                        if len(encoded) > 255:
                            encoded = encoded[:255]
                        ser.write(bytes([len(encoded)]))
                        ser.write(encoded)
                
                ser.flush()
                
                # Read response — skip any unsolicited frames (STATUS text, RTC
                # replies, ...) so they can't be mistaken for the connect result.
                resp_type, status_text = self._read_response(
                    {ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Connected"}
                else:  # ERROR
                    self._read_byte(timeout_s)
                    msg = "Connection failed"
                    if status_text:
                        msg = f"Connection failed — device said: {status_text}"
                    return {"success": False, "ssid": ssid, "message": msg}
            except Exception as e:
                return {"success": False, "ssid": ssid, "message": str(e)}

    def wifi_disconnect(self, timeout_s: float = 10.0) -> dict:
        """Disconnect WiFi"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.WIFI_DISCONNECT)
                
                # Skip unsolicited frames, then read the result
                resp_type, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "message": "Disconnected"}
                else:  # ERROR
                    self._read_byte(timeout_s)
                    return {"success": False, "message": "Disconnect failed"}
            except Exception as e:
                return {"success": False, "message": str(e)}

    def wifi_get_status(self, timeout_s: float = 5.0) -> dict:
        """Get WiFi status"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            self._send_cmd(ProtoCmd.WIFI_STATUS)
            
            # Wait for a real WIFI_INFO frame, skipping any unsolicited frames
            # (STATUS text, RTC replies, ...) that arrive in between.
            # NOTE: this raises RuntimeError on timeout / no valid response.
            # Callers must treat that as "status unknown" — NOT as "device is
            # disconnected" (the device is regularly busy for several seconds
            # right after connecting, e.g. while syncing the RTC from NTP).
            resp_type, _ = self._read_response({ProtoResp.WIFI_INFO}, timeout_s)
            
            connected = self._read_byte(timeout_s)
            
            if not connected:
                return {"ssid": "", "rssi": ""}
            
            rssi_byte = self._read_byte(timeout_s)
            rssi = rssi_byte if rssi_byte < 128 else rssi_byte - 256
            
            ssid = self._read_string(timeout_s)
            self._read_string(timeout_s)  # IP
            self._read_string(timeout_s)  # Gateway
            self._read_string(timeout_s)  # DNS
            
            return {"ssid": ssid, "rssi": str(rssi)}

    def wifi_get_saved_networks(self, timeout_s: float = 5.0) -> list[str]:
        """Get saved WiFi networks"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.WIFI_SAVED_LIST)
                
                # Skip unsolicited frames, then read the saved list
                resp_type, _ = self._read_response(
                    {ProtoResp.WIFI_SAVED, ProtoResp.ERROR}, timeout_s)
                
                if resp_type == ProtoResp.ERROR:
                    return []
                
                num_networks = self._read_byte(timeout_s)
                networks = []
                
                for _ in range(num_networks):
                    self._read_byte(timeout_s)  # rssi (unused for saved)
                    self._read_byte(timeout_s)  # security
                    ssid_len = self._read_byte(timeout_s)
                    ssid = self._read_exact(ssid_len, timeout_s).decode('utf-8', errors='replace')
                    networks.append(ssid)
                
                return networks
            except Exception:
                return []

    def wifi_connect_saved(self, ssid: str, timeout_s: float = 60.0) -> dict:
        """Connect to a previously saved WiFi network by SSID."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.WIFI_CONNECT_SAVED)
                # Send SSID
                encoded = ssid.encode('utf-8')
                if len(encoded) > 255:
                    encoded = encoded[:255]
                ser.write(bytes([len(encoded)]))
                ser.write(encoded)
                ser.flush()
                
                # Skip unsolicited frames, then read the connect result
                resp_type, status_text = self._read_response(
                    {ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Connected"}
                else:  # ERROR
                    self._read_byte(timeout_s)
                    msg = "Connection failed"
                    if status_text:
                        msg = f"Connection failed — device said: {status_text}"
                    return {"success": False, "ssid": ssid, "message": msg}
            except Exception as e:
                return {"success": False, "ssid": ssid, "message": str(e)}

    def wifi_forget_by_ssid(self, ssid: str, timeout_s: float = 10.0) -> dict:
        """Forget a saved WiFi network by SSID."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.WIFI_FORGET)
                
                # Send SSID
                encoded = ssid.encode('utf-8')
                if len(encoded) > 255:
                    encoded = encoded[:255]
                ser.write(bytes([len(encoded)]))
                ser.write(encoded)
                ser.flush()
                
                # Skip unsolicited frames, then read the result
                resp_type, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Network forgotten"}
                else:  # ERROR
                    error_code = self._read_byte(timeout_s)
                    return {"success": False, "ssid": ssid, "message": f"Error code {error_code}"}
            except Exception as e:
                return {"success": False, "ssid": ssid, "message": str(e)}

    def wifi_reset(self, timeout_s: float = 10.0) -> dict:
        """Forget all saved WiFi networks by forgetting each one."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        saved = self.wifi_get_saved_networks(timeout_s=timeout_s)
        for ssid in saved:
            self.wifi_forget_by_ssid(ssid, timeout_s=timeout_s)
        return {"success": True, "message": f"Forgot {len(saved)} network(s)"}

    def poll_rtc_during_live(self, timeout_s: float = 1.0) -> dict:
        """Poll RTC time during live streaming without interrupting the stream.
        
        This sends RTC_TIME command and reads the response, but does NOT:
        - Take the lock (would deadlock with _live_loop)
        - Reset input buffer (would lose live data)
        
        Safe to call from GUI event handlers while live streaming is active.
        """
        if not self.is_open:
            return {"success": False, "time": ""}
        
        try:
            ser = self._require_open()
            # Send RTC_TIME command WITHOUT lock (would deadlock)
            ser.write(bytes([ProtoCmd.RTC_TIME]))
            ser.flush()
            
            # Try to read response (with short timeout)
            resp_type = self._read_byte(timeout_s)
            
            if resp_type != ProtoResp.RTC_RESPONSE:
                return {"success": False, "time": ""}
            
            # Read the length-prefixed time string
            time_str = self._read_string(timeout_s)
            
            return {"success": True, "time": time_str}
        except Exception as e:
            return {"success": False, "time": ""}

    def get_rtc_time(self, timeout_s: float = 3.0) -> dict:
        """Get RTC time when live streaming is NOT active."""
        if not self.is_open:
            return {"success": False, "time": ""}
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.RTC_TIME)
                
                # Skip unsolicited frames, then read the RTC response
                resp_type, _ = self._read_response({ProtoResp.RTC_RESPONSE}, timeout_s)
                
                time_str = self._read_string(timeout_s)
                return {"success": True, "time": time_str}
            except Exception:
                return {"success": False, "time": ""}

    def drain_status_messages(self, listen_s: float = 1.5) -> list[str]:
        """Read any unsolicited 0x02 STATUS messages waiting in the buffer.
        Returns list of message strings found within the listen window."""
        messages = []
        if not self.is_open:
            return messages
        with self.lock:
            ser = self._require_open()
            deadline = time.time() + listen_s
            while time.time() < deadline:
                b = ser.read(1)
                if not b:
                    continue
                if b[0] != 0x02:  # STATUS type
                    # Not a STATUS message — put back concept: just discard non-STATUS bytes
                    continue
                # Read length
                lb = ser.read(1)
                if not lb:
                    break
                length = lb[0]
                if length == 0:
                    continue
                data = ser.read(length)
                if data:
                    messages.append(data.decode("utf-8", errors="replace"))
        return messages

    def pump_set(self, percent: int, timeout_s: float = 3.0) -> dict:
        """Set the pump speed (0-100%)."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        pct = max(0, min(100, percent))
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.PUMP_SET)
            ser.write(bytes([pct]))
            ser.flush()
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True, "percent": pct}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def pump_get(self, timeout_s: float = 3.0) -> dict:
        """Get the current pump speed."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.PUMP_GET)
            resp, _ = self._read_response({ProtoResp.PUMP_STATUS, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            length = self._read_byte(timeout_s)
            pct = self._read_byte(timeout_s) if length >= 1 else 0
            if length > 1:                       # tolerate future extra fields
                self._read_exact(length - 1, timeout_s)
            return {"success": True, "percent": pct}

    def calib_multipoint(self, channel: int, r2ppm_kohm: float, alpha: float,
                          timeout_s: float = 5.0) -> dict:
        """Send CALIB_MULTIPOINT (0x23): store pre-fitted R2ppm and alpha in sensor EEPROM."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.CALIB_MULTIPOINT)
            ser.write(bytes([channel & 0xFF]))
            ser.write(struct.pack('<f', r2ppm_kohm))
            ser.write(struct.pack('<f', alpha))
            ser.flush()
            resp = self._read_byte(timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            elif resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            return {"success": False, "message": f"Unexpected response: {resp}"}

    def calib_timestamp(self, channel: int, epoch: int, timeout_s: float = 8.0) -> dict:
        """Send CALIB_TIMESTAMP (0x2A): store the calibration unix time (UTC) in sensor EEPROM.

        Sent automatically right after a successful calib_multipoint so the
        calibration date can be shown on later EEPROM reads.
        """
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.CALIB_TIMESTAMP)
            ser.write(bytes([channel & 0xFF]))
            ser.write(struct.pack('<I', epoch & 0xFFFFFFFF))
            ser.flush()
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def lora_info(self, timeout_s: float = 10.0) -> dict:
        """Read DevEUI from module + AppEUI/AppKey from device NVS."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.LORA_INFO)
            resp = self._read_byte(timeout_s)
            if resp == ProtoResp.LORA_INFO:
                length = self._read_byte(timeout_s)
                payload = self._read_exact(length, timeout_s).decode("utf-8", errors="replace")
                parts = payload.split(",", 2)
                return {
                    "success": True,
                    "dev_eui": parts[0] if len(parts) > 0 else "",
                    "app_eui": parts[1] if len(parts) > 1 else "",
                    "app_key": parts[2] if len(parts) > 2 else "",
                }
            elif resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            return {"success": False, "message": f"Unexpected response: {resp}"}

    def gen_lora_keys(self, timeout_s: float = 15.0) -> dict:
        """Generate random AppEUI + AppKey, program LoRa module, store in device NVS."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.LORA_GEN_KEYS)
            resp = self._read_byte(timeout_s)
            if resp == ProtoResp.LORA_INFO:
                length = self._read_byte(timeout_s)
                payload = self._read_exact(length, timeout_s).decode("utf-8", errors="replace")
                parts = payload.split(",", 2)
                return {
                    "success": True,
                    "dev_eui": parts[0] if len(parts) > 0 else "",
                    "app_eui": parts[1] if len(parts) > 1 else "",
                    "app_key": parts[2] if len(parts) > 2 else "",
                }
            elif resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            return {"success": False, "message": f"Unexpected response: {resp}"}

    def eeprom_read_ids(self, channel: int, timeout_s: float = 5.0) -> dict:
        """Send EEPROM_READ (0x27): read current TGS ID, lot #, board ID and last
        calibration date/time from device."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.EEPROM_READ)
            ser.write(bytes([channel & 0xFF]))
            ser.flush()
            # _read_response consumes interleaved LIVE_DATA/STATUS frames so
            # streaming output can never be mistaken for the reply.
            resp, _ = self._read_response(ProtoResp.EEPROM_IDS, timeout_s)
            length = self._read_byte(timeout_s)
            payload = self._read_exact(length, timeout_s).decode("utf-8", errors="replace")
            parts = payload.split(",", 4)
            return {
                "success": True,
                "tgs_id": parts[0] if len(parts) > 0 else "",
                "crc": parts[1] if len(parts) > 1 else "",
                "lot": parts[2] if len(parts) > 2 else "",
                "board": parts[3] if len(parts) > 3 else "",
                # Decimal unix time of last calibration; "" = never, "ERR" = bad CRC
                "calib_time": parts[4] if len(parts) > 4 else "",
            }

    def eeprom_write_ids(self, channel: int, tgs_id: int, lot: str, board: str,
                         timeout_s: float = 8.0) -> dict:
        """Send EEPROM_WRITE (0x26): write TGS ID, lot #, board ID to sensor EEPROM."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        lot_b = lot.encode("ascii", errors="ignore")[:16]
        board_b = board.encode("ascii", errors="ignore")[:32]
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.EEPROM_WRITE)
            ser.write(bytes([channel & 0xFF]))
            ser.write(bytes([(tgs_id >> 8) & 0xFF, tgs_id & 0xFF]))
            ser.write(bytes([len(lot_b)]))
            ser.write(lot_b)
            ser.write(bytes([len(board_b)]))
            ser.write(board_b)
            ser.flush()
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def eeprom_clear_ids(self, channel: int, timeout_s: float = 8.0) -> dict:
        """Send EEPROM_CLEAR (0x28): erase TGS ID / lot # / board ID blocks."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.EEPROM_CLEAR)
            ser.write(bytes([channel & 0xFF]))
            ser.flush()
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def fw_version(self, timeout_s: float = 5.0) -> dict:
        """Send FW_VERSION (0x29): read the firmware version string from the device.

        Powers the Settings > About compatibility check (run on connect).
        """
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.FW_VERSION)
            resp, _ = self._read_response({ProtoResp.FW_VERSION, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.FW_VERSION:
                length = self._read_byte(timeout_s)
                version = self._read_exact(length, timeout_s).decode("utf-8", errors="replace")
                return {"success": True, "version": version.strip()}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def board_id_write(self, board_id: str, timeout_s: float = 5.0) -> dict:
        """Send BOARD_ID_WRITE (0x2B): store the mainboard ID in ESP32 NVS."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        bid_b = board_id.encode("ascii", errors="ignore")[:32]
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.BOARD_ID_WRITE)
            ser.write(bytes([len(bid_b)]))
            ser.write(bid_b)
            ser.flush()
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def board_id_read(self, timeout_s: float = 5.0) -> dict:
        """Send BOARD_ID_READ (0x2C): read the mainboard ID from ESP32 NVS.

        board_id is "" when the ID has never been set.
        """
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.BOARD_ID_READ)
            resp, _ = self._read_response({ProtoResp.BOARD_ID, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.BOARD_ID:
                length = self._read_byte(timeout_s)
                board_id = self._read_exact(length, timeout_s).decode("utf-8", errors="replace")
                return {"success": True, "board_id": board_id}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}

    def board_id_clear(self, timeout_s: float = 5.0) -> dict:
        """Send BOARD_ID_CLEAR (0x2D): erase the mainboard ID from ESP32 NVS."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.BOARD_ID_CLEAR)
            resp, _ = self._read_response({ProtoResp.OK, ProtoResp.ERROR}, timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True}
            code = self._read_byte(timeout_s)
            return {"success": False, "message": f"Error code {code}"}


# ============ GUI APPLICATION ============

class _StatsRow(ctk.CTkFrame):
    """Min/avg/max readout: one row when it fits, stacked when the card is narrow."""

    def __init__(self, master) -> None:
        super().__init__(master, fg_color="transparent")
        self._labels = [
            ctk.CTkLabel(self, text="", font=ui_font(12), text_color="#6b7280", anchor="w", height=16)
            for _ in range(3)
        ]
        self._stacked: bool | None = None
        self._layout(False)
        self.bind("<Configure>", self._on_resize)

    def _layout(self, stacked: bool) -> None:
        if stacked == self._stacked:
            return
        self._stacked = stacked
        for i, lbl in enumerate(self._labels):
            lbl.grid_forget()
            if stacked:
                lbl.grid(row=i, column=0, sticky="w")
            else:
                lbl.grid(row=0, column=i, sticky="w", padx=(0 if i == 0 else 10, 0))

    def _on_resize(self, event) -> None:
        needed = sum(lbl.winfo_reqwidth() for lbl in self._labels) + 20
        if self._stacked and event.width >= needed + 12:
            self._layout(False)
        elif not self._stacked and event.width < needed:
            self._layout(True)

    def set_stats(self, lo: str, avg: str, hi: str) -> None:
        for lbl, text in zip(self._labels, (f"min {lo}", f"avg {avg}", f"max {hi}")):
            lbl.configure(text=text)

    def clear(self) -> None:
        for lbl in self._labels:
            lbl.configure(text="")


class App(ctk.CTk):

    # Choose which readings to hide from the raw data log table
    HIDDEN_LOG_COLS: frozenset[str] = frozenset({
        "scd4x_1_t",
        "scd4x_1_rh",
        "sht45_1_t_avg", 
        "lps22df_1_t_avg",
        "tgs2611_1_v_avg", 
        "tgs2611_1_rs_avg",
        "sfm3505_1_o2_slm_avg",
    })

    GAS_COLS: dict = {
        "scd4x_1_co2":       ("CO\u2082",    "ppm"),
        "sht45_1_rh_avg":    ("Humidity",    "%RH"),
        "tmp117_1_t_avg":     ("Temperature", "\u00b0C"),
        "lps22df_1_p_avg":    ("Pressure",    "hPa"),
        "tgs2611_1_ppm_avg": ("CH\u2084",      "ppm"),
        "tgs2611_1_raw_avg": ("CH\u2084 Raw", ""),
        "sfm3505_1_o2_slm_avg": ("Flow", "SLM"),
        "n2o_uart_ppm_avg":     ("N\u2082O",    "ppm"),
    }

    # A gas card greys out if it has had no valid reading for this long
    GAS_CARD_STALE_S = 30

    C_CARD_NA = "#6b7280"
    C_CARD_NEUTRAL = "#2563eb"
    C_CARD_BG = "#ffffff"
    C_CARD_BORDER_NA = "#e5e7eb"
    C_CARD_BORDER_NEUTRAL = "#bfdbfe"

    LORA_COLS: dict = {
        "scd4x_1_co2":      ("CO\u2082", "ppm"),
        "sht45_1_rh_avg": ("Humidity", "%RH"),
        "tmp117_1_t_avg": ("Temperature", "\u00b0C"),
        "lps22df_1_p_avg": ("Pressure", "hPa"),
        "tgs2611_1_ppm_avg": ("CH\u2084", "ppm"),
        "tgs2611_1_raw_avg": ("CH\u2084 Raw", ""),
        "sfm3505_1_air_slm_avg":      ("Flow", "SLM"),
        "n2o_uart_ppm_avg":      ("N\u2082O", "ppm"),
    }


    def __init__(self) -> None:
        super().__init__()

        self.title("GEM GUI")
        if APP_ICON_PATH:
            # CustomTkinter installs its own icon shortly after start-up, so ours
            # has to be applied afterwards.
            self.after(250, lambda: self.iconbitmap(str(APP_ICON_PATH)))

        self.geometry("900x680")
        self.minsize(850, 620)

        self.c_bg = C_BG
        self.c_surface = C_SURFACE
        self.c_text = C_TEXT
        self.c_muted = C_MUTED
        self.c_accent = C_ACCENT
        self.c_accent_hover = C_ACCENT_HOVER
        self.c_accent_press = C_ACCENT_HOVER
        self.c_border = C_BORDER
        self.c_row_alt = C_ROW_ALT
        self.c_tab_idle = C_TAB_IDLE

        self.client = SerialMenuClient()
        self.events: Queue[tuple[str, object]] = Queue()
        self.file_entries: list[FileEntry] = []
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
        self.live_last_values: dict[str, tuple[float, float]] = {}
        self.live_hold_timeout_s = 15.0
        self.live_autoscroll = tk.BooleanVar(value=True)
        self.gas_card_stats: dict[str, list[float]] = {}
        self.gas_card_widgets: dict[str, tuple[ctk.CTkLabel, ctk.CTkLabel, ctk.CTkFrame, _StatsRow]] = {}
        self.gas_card_age_labels: dict[str, ctk.CTkLabel] = {}
        # time.monotonic() of each card's last valid reading
        self.gas_card_last_update: dict[str, float] = {}

        # TTN / MQTT (LoRaWAN cloud) state
        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_devices: dict[str, dict] = {}
        self.mqtt_history: dict[str, dict[str, deque]] = {}
        self.mqtt_selected_device = tk.StringVar(value="")
        self.mqtt_card_widgets: dict[str, tuple[ctk.CTkLabel, ctk.CTkLabel, ctk.CTkFrame]] = {}
        self.mqtt_graph_canvases: dict[str, tk.Canvas] = {}
        self.mqtt_history_limit = 300
        self.mqtt_row_counter = 0
        self.mqtt_data_dir = Path(__file__).resolve().parent / "lora_data"
        self.mqtt_table_row_limit = 500


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
        self.wifi_form_password: ctk.CTkEntry | None = None
        self.wifi_form_username: ctk.CTkEntry | None = None
        self.wifi_form_ssid: ctk.CTkEntry | None = None
        self.wifi_form_security: ctk.CTkComboBox | None = None
        self.wifi_form_fields_frame: ctk.CTkFrame | None = None
        
        # RTC settings
        self.rtc_poll_timer: int | None = None
        self.rtc_poll_inflight = False
        self.rtc_current_datetime = "0000-00-00 00:00"

        # Pump settings — the slider controls the pump speed directly.
        self._pump_pending: int | None = None        # newest slider value, not sent yet
        self._pump_debounce_id: str | None = None    # pending debounce timer id
        self._pump_send_inflight = False
        self._pump_syncing = False                   # true while applying a device value

        # LoRa settings
        self.lora_dev_eui_var: tk.StringVar | None = None
        self.lora_app_eui_var: tk.StringVar | None = None
        self.lora_app_key_var: tk.StringVar | None = None
        self.lora_status_var: tk.StringVar | None = None

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

    def _on_port_selected(self, _value=None) -> None:
        self.focus_set()

    def _configure_theme(self) -> None:
        """Window background plus the ttk styles for the Treeview tables.

        CustomTkinter has no table widget, so tables stay ttk.Treeview; they are
        restyled here to match the CustomTkinter widgets.
        """
        self.configure(fg_color=self.c_bg)

        self.style = ttk.Style(self)
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")

        self.style.configure(
            "Treeview",
            background=self.c_surface,
            fieldbackground=self.c_surface,
            foreground=self.c_text,
            rowheight=26,
            borderwidth=0,
            font=("Segoe UI", 10),
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

    def _make_paned(self, master, orient: str) -> tk.PanedWindow:
        """Resizable split pane (CustomTkinter has no paned window)."""
        return tk.PanedWindow(
            master, orient=orient, bg=self.c_bg, bd=0, sashwidth=6, sashrelief=tk.FLAT, opaqueresize=True,
        )

    def _make_scrollbar(self, master, orientation: str, command) -> ctk.CTkScrollbar:
        return ctk.CTkScrollbar(master, orientation=orientation, command=command)

    def _make_card(self, master) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            master,
            fg_color=self.C_CARD_BG,
            border_width=1,
            border_color=self.C_CARD_BORDER_NA,
            corner_radius=10,
        )

    def _make_graph_canvas(self, master, height: int) -> tk.Canvas:
        """Plain canvas used for the line graphs, wired for the hover tooltip."""
        canvas = tk.Canvas(
            master, bg=self.c_surface, highlightthickness=1, highlightbackground=self.c_border, height=height,
        )
        canvas.bind("<Motion>", lambda e, c=canvas: self._on_graph_hover(e, c))
        canvas.bind("<Leave>", lambda e, c=canvas: c.delete("hover"))
        return canvas

    def _apply_tree_stripes(self, tree: ttk.Treeview, rows: list[tuple]) -> None:
        tree.tag_configure("even", background=self.c_surface)
        tree.tag_configure("odd", background=self.c_row_alt)
        for i, values in enumerate(rows):
            tag = "even" if i % 2 == 0 else "odd"
            tree.insert("", tk.END, values=values, tags=(tag,))

    def _build_ui(self) -> None:
        top = make_frame(self)
        top.pack(fill=tk.X, padx=10, pady=10)

        make_label(top, "Port:", font=ui_font(13, "bold")).pack(side=tk.LEFT)
        self.port_combo = ctk.CTkComboBox(
            top, variable=self.port_var, values=[], width=170, state="readonly",
            font=ui_font(13), dropdown_font=ui_font(13), command=self._on_port_selected,
        )
        self.port_combo.pack(side=tk.LEFT, padx=(6, 10))

        AccentButton(top, text="↻  Refresh Ports", command=self.refresh_ports).pack(side=tk.LEFT)

        make_label(top, "Baud:", font=ui_font(13, "bold")).pack(side=tk.LEFT, padx=(14, 0))
        _baud_rates = ["9600", "19200", "38400", "57600", "115200"]
        self.baud_combo = ctk.CTkComboBox(
            top, variable=self.baud_var, values=_baud_rates, width=100, state="readonly",
            font=ui_font(13), dropdown_font=ui_font(13),
        )
        self.baud_combo.pack(side=tk.LEFT, padx=(6, 10))

        self.connect_btn = AccentButton(top, text="🔗  Connect", command=self.toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))

        make_label(top, textvariable=self.status_var, text_color=self.c_muted).pack(side=tk.LEFT, padx=(8, 0))

        # Right side: WiFi status and RTC time
        right_frame = make_frame(top)
        right_frame.pack(side=tk.RIGHT, padx=(8, 0))

        wifi_icon_font = ctk.CTkFont(family=WIFI_FONT_NAME, size=13) if WIFI_FONT_PATH else ui_font(13)
        small_font = ui_font(12)

        wifi_frame = make_frame(right_frame)
        wifi_frame.pack(side=tk.LEFT)

        self.rtc_time_label = make_label(
            right_frame, self.rtc_current_datetime, font=small_font, text_color=self.c_muted)
        self.rtc_time_label.pack(side=tk.LEFT, padx=(12, 0))

        self.wifi_name_label = make_label(wifi_frame, "", font=small_font, text_color=self.c_muted)
        self.wifi_name_label.pack(side=tk.LEFT)

        self.wifi_icon_label = make_label(wifi_frame, "", font=wifi_icon_font, text_color=self.c_muted)
        self.wifi_icon_label.pack(side=tk.LEFT, padx=(4, 0))

        body = make_frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        sidebar = ctk.CTkFrame(body, fg_color=self.c_surface, width=170, corner_radius=10)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        content = make_frame(body)
        content.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self.live_tab = make_frame(content)
        self.files_tab = make_frame(content)
        self.mqtt_tab = make_frame(content)
        self.wifi_tab = make_frame(content)

        for page in (self.live_tab, self.files_tab, self.mqtt_tab, self.wifi_tab):
            page.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

        self.sidebar_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("live", "▶  Live Data", self.live_tab),
            ("files", "🗂  Files", self.files_tab),
            ("mqtt", "📡  TTN Live", self.mqtt_tab),
            ("settings", "⚙  Settings", self.wifi_tab),
        ]
        for key, label, frame in nav_items:
            btn = ctk.CTkButton(
                sidebar,
                text=label,
                anchor="w",
                fg_color="transparent",
                text_color=self.c_text,
                hover_color=self.c_tab_idle,
                font=ui_font(13),
                height=40,
                corner_radius=8,
                command=lambda f=frame, k=key: self._show_page(f, k),
            )
            btn.pack(fill=tk.X, padx=6, pady=(6, 0))
            self.sidebar_buttons[key] = btn

        self._build_live_tab()
        self._build_files_tab()
        self._build_mqtt_tab()
        self._build_wifi_tab()

        self._show_page(self.live_tab, "live")

        # Initialize WiFi status display
        self._update_wifi_top_bar()


    def _show_page(self, frame: ctk.CTkFrame, key: str) -> None:
        frame.tkraise()
        for k, btn in self.sidebar_buttons.items():
            if k == key:
                btn.configure(fg_color=self.c_accent, hover_color=self.c_accent_hover, text_color="#ffffff")
            else:
                btn.configure(fg_color="transparent", hover_color=self.c_tab_idle, text_color=self.c_text)


    def _build_live_tab(self) -> None:
        # The whole page scrolls so cards, graphs and the log keep a readable size.
        live_page = ctk.CTkScrollableFrame(self.live_tab, fg_color="transparent")
        live_page.pack(fill=tk.BOTH, expand=True)

        btns = make_frame(live_page)
        btns.pack(fill=tk.X)
        self.live_start_btn = AccentButton(
            btns,
            text="▶  Start Live",
            command=self.start_live,
            state=tk.DISABLED,
        )
        self.live_start_btn.pack(side=tk.LEFT)
        self.live_stop_btn = AccentButton(btns, text="⏹  Stop Live", command=self.stop_live, state=tk.DISABLED)
        self.live_stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        # Gas reading cards
        cards_outer = make_frame(live_page)
        cards_outer.pack(fill=tk.X, pady=(10, 12))

        make_label(
            cards_outer,
            "Current Readings",
            font=ui_font(15, "bold"),
        ).pack(anchor="w", padx=4, pady=(0, 6))

        cards_row = make_frame(cards_outer)
        cards_row.pack(fill=tk.X)

        cards_per_row = 4
        for c in range(cards_per_row):
            cards_row.columnconfigure(c, weight=1, uniform="gas_cards")

        for i, (col_name, (label, unit)) in enumerate(self.GAS_COLS.items()):
            card = self._make_card(cards_row)
            row, col = divmod(i, cards_per_row)
            card.grid(row=row, column=col, sticky="nsew", padx=(0 if col == 0 else 8, 0), pady=(0 if row == 0 else 8, 0))

            make_label(
                card,
                label,
                font=ui_font(12, "bold"),
                text_color="#6b7280",
            ).pack(anchor="w", padx=12, pady=(10, 0))

            val_frame = make_frame(card)
            val_frame.pack(anchor="w", padx=12)

            val_label = make_label(
                val_frame,
                "N/A",
                font=ui_font(37, "bold"),
                text_color=self.C_CARD_NA,
            )

            val_label.pack(side=tk.LEFT, anchor="s")

            unit_label = make_label(
                val_frame,
                f" {unit}",
                font=ui_font(16),
                text_color="#9ca3af",
            )
            unit_label.pack(side=tk.LEFT, anchor="s", pady=(0, 6))

            stats_label = _StatsRow(card)
            stats_label.pack(fill=tk.X, padx=12, pady=(2, 0))

            age_label = make_label(
                card,
                "No data yet",
                font=ui_font(12),
                text_color="#9ca3af",
            )
            age_label.pack(anchor="w", padx=12, pady=(2, 10))

            self.gas_card_widgets[col_name] = (val_label, unit_label, card, stats_label)
            self.gas_card_age_labels[col_name] = age_label

        self.after(1000, self._tick_gas_card_ages)

        # Graphs on top, table below; the page scrolls instead of a split pane
        graphs_frame = make_frame(live_page)
        graphs_frame.pack(fill=tk.X, pady=(10, 0))

        make_label(graphs_frame, "Live Graphs", font=ui_font(13, "bold")).pack(anchor="w")

        self.graphs_content = make_frame(graphs_frame)
        self.graphs_content.pack(fill=tk.X, expand=True)

        self.graph_names: list[str] = []
        self.graph_canvases: list[tk.Canvas] = []
        self.graph_hover_data: dict = {}
        self._graph_redraw_pending = False

        # Bottom: Table
        table_frame = make_frame(live_page)
        table_frame.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        table_header = make_frame(table_frame)
        table_header.pack(fill=tk.X)
        make_label(table_header, "Raw Data Log", font=ui_font(13, "bold")).pack(side=tk.LEFT)
        ctk.CTkCheckBox(
            table_header,
            text="Auto-scroll",
            variable=self.live_autoscroll,
            font=ui_font(13),
            text_color=self.c_text,
            checkbox_width=20,
            checkbox_height=20,
        ).pack(side=tk.LEFT, padx=(12, 0))

        table_inner = make_frame(table_frame)
        table_inner.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        table_inner.rowconfigure(0, weight=1)
        table_inner.columnconfigure(0, weight=1)

        self.live_table = ttk.Treeview(table_inner, show="headings", height=10)
        self.live_table.grid(row=0, column=0, sticky="nsew")

        scroll_y = self._make_scrollbar(table_inner, "vertical", self.live_table.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = self._make_scrollbar(table_inner, "horizontal", self.live_table.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.live_table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        self.live_table_columns = []
        self.live_table_col_indices: list[int] = []


    def _build_mqtt_tab(self) -> None:
        conn = make_frame(self.mqtt_tab)
        conn.pack(fill=tk.X)

        make_label(conn, "Broker").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.mqtt_broker_var = tk.StringVar(value="eu1.cloud.thethings.network")
        ctk.CTkEntry(conn, textvariable=self.mqtt_broker_var, width=240, font=ui_font(13)).grid(row=0, column=1, padx=(0, 10))

        make_label(conn, "Port").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.mqtt_port_var = tk.StringVar(value="1883")
        ctk.CTkEntry(conn, textvariable=self.mqtt_port_var, width=70, font=ui_font(13)).grid(row=0, column=3, padx=(0, 10))

        make_label(conn, "Application ID").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        self.mqtt_app_id_var = tk.StringVar(value="")
        ctk.CTkEntry(conn, textvariable=self.mqtt_app_id_var, width=240, font=ui_font(13)).grid(row=1, column=1, pady=(6, 0), padx=(0, 10))

        make_label(conn, "Tenant").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=(6, 0))
        self.mqtt_tenant_var = tk.StringVar(value="ttn")
        ctk.CTkEntry(conn, textvariable=self.mqtt_tenant_var, width=70, font=ui_font(13)).grid(row=1, column=3, pady=(6, 0), padx=(0, 10))

        make_label(conn, "API Key").grid(row=2, column=0, sticky="w", padx=(0, 4), pady=(6, 0))
        self.mqtt_api_key_var = tk.StringVar(value="")
        ctk.CTkEntry(conn, textvariable=self.mqtt_api_key_var, width=360, show="*", font=ui_font(13)).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(6, 0), padx=(0, 10)
        )

        btns = make_frame(conn)
        btns.grid(row=0, column=4, rowspan=3, padx=(16, 0), sticky="n")
        self.mqtt_connect_btn = AccentButton(btns, text="🔗  Connect", command=self.mqtt_connect)
        self.mqtt_connect_btn.pack(fill=tk.X)
        self.mqtt_disconnect_btn = AccentButton(
            btns, text="✖  Disconnect", command=self.mqtt_disconnect, state=tk.DISABLED
        )
        self.mqtt_disconnect_btn.pack(fill=tk.X, pady=(4, 0))

        if not HAS_PAHO_MQTT:
            self.mqtt_connect_btn.configure(state=tk.DISABLED)
            make_label(
                conn,
                "Install paho-mqtt to enable this tab: pip install paho-mqtt",
                text_color="#dc2626",
            ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))

        status_row = make_frame(self.mqtt_tab)
        status_row.pack(fill=tk.X, pady=(8, 0))
        self.mqtt_status_var = tk.StringVar(value="Disconnected")
        make_label(status_row, textvariable=self.mqtt_status_var).pack(side=tk.LEFT)

        make_label(status_row, "Device:").pack(side=tk.LEFT, padx=(20, 4))
        self.mqtt_device_combo = ctk.CTkComboBox(
            status_row, variable=self.mqtt_selected_device, state="readonly", width=200, values=[],
            font=ui_font(13), dropdown_font=ui_font(13),
            command=lambda _v: self._refresh_mqtt_display(),
        )
        self.mqtt_device_combo.pack(side=tk.LEFT)

        cards_outer = make_frame(self.mqtt_tab)
        cards_outer.pack(fill=tk.X, pady=(12, 12))
        make_label(cards_outer, "Current Readings", font=ui_font(15, "bold")).pack(anchor="w", padx=4, pady=(0, 6))
        cards_row = make_frame(cards_outer)
        cards_row.pack(fill=tk.X)

        for col_name, (label, unit) in self.LORA_COLS.items():
            card = self._make_card(cards_row)
            card.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 10))

            make_label(card, label, font=ui_font(12), text_color="#6b7280").pack(anchor="w", padx=10, pady=(10, 0))
            val_frame = make_frame(card)
            val_frame.pack(anchor="w", padx=10, pady=(0, 10))
            val_label = make_label(val_frame, "N/A", font=ui_font(29, "bold"), text_color=self.C_CARD_NA)
            val_label.pack(side=tk.LEFT, anchor="s")
            unit_label = make_label(val_frame, f" {unit}", font=ui_font(14), text_color="#9ca3af")
            unit_label.pack(side=tk.LEFT, anchor="s", pady=(0, 3))

            self.mqtt_card_widgets[col_name] = (val_label, unit_label, card)

        split = self._make_paned(self.mqtt_tab, tk.VERTICAL)
        split.pack(fill=tk.BOTH, expand=True)

        graphs_frame = make_frame(split)
        split.add(graphs_frame, stretch="always", minsize=120)
        make_label(graphs_frame, "Live Graphs", font=ui_font(13, "bold")).pack(anchor="w")

        self.mqtt_graphs_content = ctk.CTkScrollableFrame(graphs_frame, fg_color="transparent")
        self.mqtt_graphs_content.pack(fill=tk.BOTH, expand=True)

        self.mqtt_graphs_content.columnconfigure(0, weight=1)
        self.mqtt_graphs_content.columnconfigure(1, weight=1)

        for i, (col_name, (label, unit)) in enumerate(self.LORA_COLS.items()):
            row, col = divmod(i, 2)
            self.mqtt_graphs_content.rowconfigure(row, weight=1)
            pane = Section(self.mqtt_graphs_content, f"{label} ({unit})" if unit else label)
            pane.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            canvas = self._make_graph_canvas(pane, height=160)
            canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 8))
            self.mqtt_graph_canvases[col_name] = canvas


        table_frame = make_frame(split)
        split.add(table_frame, stretch="always", minsize=80)
        make_label(table_frame, "Raw Uplinks", font=ui_font(13, "bold")).pack(anchor="w")

        table_cols = ["timestamp", "device_id"] + list(self.LORA_COLS.keys())
        self.mqtt_table = ttk.Treeview(table_frame, columns=table_cols, show="headings", height=8)
        for c in table_cols:
            self.mqtt_table.heading(c, text=c)
            self.mqtt_table.column(c, width=90, anchor="center")
        self.mqtt_table.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        mqtt_table_vsb = self._make_scrollbar(table_frame, "vertical", self.mqtt_table.yview)
        mqtt_table_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.mqtt_table.configure(yscrollcommand=mqtt_table_vsb.set)


    def _build_files_tab(self) -> None:
        btns = make_frame(self.files_tab)
        btns.pack(fill=tk.X)
        self.refresh_files_btn = AccentButton(
            btns, text="Refresh File List", command=self.refresh_files, state=tk.DISABLED
        )
        self.refresh_files_btn.pack(side=tk.LEFT)
        self.select_all_btn = AccentButton(
            btns, text="Select All", command=self._select_all_files, state=tk.DISABLED
        )
        self.select_all_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.download_btn = AccentButton(
            btns, text="Download Selected CSV(s)", command=self.download_selected, state=tk.DISABLED
        )
        self.download_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = self._make_paned(self.files_tab, tk.VERTICAL)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        top = make_frame(split)
        bottom = make_frame(split)
        split.add(top, stretch="always", minsize=80)
        split.add(bottom, stretch="always", minsize=160)

        files_wrap = make_frame(top)
        files_wrap.pack(fill=tk.BOTH, expand=True)
        files_wrap.rowconfigure(0, weight=1)
        files_wrap.columnconfigure(0, weight=1)

        self.files_tree = ttk.Treeview(files_wrap, columns=("idx", "path", "size"), show="headings", selectmode="extended")
        self.files_tree.heading("idx", text="#", anchor="center")
        self.files_tree.heading("path", text="Path", anchor="w")
        self.files_tree.heading("size", text="Bytes", anchor="e")
        self.files_tree.column("idx", width=60, anchor="center", stretch=False)
        self.files_tree.column("path", width=400, anchor="w", stretch=True)
        self.files_tree.column("size", width=120, anchor="e", stretch=False)
        self.files_tree.grid(row=0, column=0, sticky="nsew")
        self.files_tree.bind("<<TreeviewSelect>>", self.on_file_selected)

        files_vsb = self._make_scrollbar(files_wrap, "vertical", self.files_tree.yview)
        files_vsb.grid(row=0, column=1, sticky="ns")
        files_hsb = self._make_scrollbar(files_wrap, "horizontal", self.files_tree.xview)
        files_hsb.grid(row=1, column=0, sticky="ew")
        self.files_tree.configure(yscrollcommand=files_vsb.set, xscrollcommand=files_hsb.set)

        make_label(bottom, textvariable=self.preview_title_var, font=ui_font(13, "bold")).pack(anchor="w")

        # Metadata / truncation info line. Packed here — *before* the expanding
        # panedwindow below — because a widget packed after an expanding sibling
        # gets clipped first when the pane is short. It is packed on demand (see
        # _set_preview_info) so an empty line costs no vertical space.
        self.preview_info_label = make_label(
            bottom, textvariable=self.preview_info_var, hint=True,
            anchor="w", justify="left")
        self.preview_info_label.bind(
            "<Configure>",
            lambda e: e.widget.configure(wraplength=max(200, e.width - 4)),
        )

        self.inner_split = self._make_paned(bottom, tk.VERTICAL)
        self.inner_split.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        graphs_frame = make_frame(self.inner_split)
        self.inner_split.add(graphs_frame, stretch="always", minsize=120)

        self.log_graphs_content = ctk.CTkScrollableFrame(graphs_frame, fg_color="transparent")
        self.log_graphs_content.pack(fill=tk.BOTH, expand=True)

        self.log_graph_canvases: list[tk.Canvas] = []
        self.log_graph_series: dict = {}

        table_frame = make_frame(self.inner_split)
        self.inner_split.add(table_frame, stretch="always", minsize=80)

        make_label(table_frame, "Raw Data Log", font=ui_font(13, "bold")).pack(anchor="w")

        preview_wrap = make_frame(table_frame)
        preview_wrap.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        preview_wrap.rowconfigure(0, weight=1)
        preview_wrap.columnconfigure(0, weight=1)

        self.preview_tree = ttk.Treeview(preview_wrap, show="headings", height=12)
        self.preview_tree.grid(row=0, column=0, sticky="nsew")

        pv_y = self._make_scrollbar(preview_wrap, "vertical", self.preview_tree.yview)
        pv_y.grid(row=0, column=1, sticky="ns")
        pv_x = self._make_scrollbar(preview_wrap, "horizontal", self.preview_tree.xview)
        pv_x.grid(row=1, column=0, sticky="ew")
        self.preview_tree.configure(yscrollcommand=pv_y.set, xscrollcommand=pv_x.set)

    def _render_log_graphs(self, graph_headers: list[str], graph_all_rows: list[list[str]]) -> None:
        for child in self.log_graphs_content.winfo_children():
            child.destroy()
        self.log_graph_canvases = []
        self.log_graph_series = {}

        if not graph_headers or not graph_all_rows:
            return

        present_cols = [col for col in self.GAS_COLS if col in graph_headers]
        if not present_cols:
            return

        ts_col = graph_headers.index("timestamp") if "timestamp" in graph_headers else None
        n = len(graph_all_rows)
        x_values = list(range(n))
        time_labels = [
            (row[ts_col] if ts_col < len(row) else str(i)) if ts_col is not None else str(i)
            for i, row in enumerate(graph_all_rows)
        ]

        self.log_graphs_content.columnconfigure(0, weight=1)
        self.log_graphs_content.columnconfigure(1, weight=1)

        for i, col_name in enumerate(present_cols):
            row_i, col_i = divmod(i, 2)
            self.log_graphs_content.rowconfigure(row_i, weight=1)

            graph_title, unit = self.GAS_COLS.get(col_name, (col_name, ""))
            pane = Section(
                self.log_graphs_content,
                f"{graph_title} ({unit})" if unit else graph_title,
            )
            pane.grid(row=row_i, column=col_i, sticky="nsew", padx=4, pady=4)

            canvas = self._make_graph_canvas(pane, height=200)
            canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 8))
            self.log_graph_canvases.append(canvas)

            col_idx = graph_headers.index(col_name)
            y_values: list[float | None] = [
                self._parse_float_or_none(row[col_idx].strip()) if col_idx < len(row) else None
                for row in graph_all_rows
            ]
            self.log_graph_series[canvas] = (x_values, time_labels, y_values)
            canvas.bind(
                "<Configure>",
                lambda e, c=canvas: self._draw_series(c, *self.log_graph_series[c])
                if e.width > 1 and e.height > 1 else None,
            )

    def _build_wifi_tab(self) -> None:
        # ---- Single scrollable canvas for the entire Settings tab ----
        _scroll_inner = ctk.CTkScrollableFrame(self.wifi_tab, fg_color="transparent")
        _scroll_inner.pack(fill=tk.BOTH, expand=True)

        # ---- Toolbar ----
        btns = make_frame(_scroll_inner)
        btns.pack(fill=tk.X, pady=(0, 10))

        self.wifi_scan_btn = AccentButton(
            btns,
            text="Scan Networks",
            command=self.wifi_scan,
            state=tk.DISABLED,
        )
        self.wifi_scan_btn.pack(side=tk.LEFT)

        self.wifi_saved_btn = AccentButton(
            btns,
            text="Saved Networks",
            command=self._toggle_saved_networks_view,
            state=tk.DISABLED,
        )
        self.wifi_saved_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.wifi_reset_btn = AccentButton(
            btns,
            text="Forget All",
            command=self.wifi_reset,
            state=tk.DISABLED,
        )
        self.wifi_reset_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = make_frame(_scroll_inner)
        split.pack(fill=tk.X)
        split.columnconfigure(0, weight=1, uniform="wifi")
        split.columnconfigure(1, weight=1, uniform="wifi")

        left = make_frame(split)
        right = make_frame(split)
        left.grid(row=0, column=0, sticky="nsew")
        right.grid(row=0, column=1, sticky="nsew")

        # Left side: Available Networks (combined list of saved + scanned + "Other")
        make_label(left, "Networks", font=ui_font(13, "bold")).pack(anchor="w")
        net_wrap = make_frame(left)
        net_wrap.pack(fill=tk.X, pady=(4, 0))
        
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
        
        net_y = self._make_scrollbar(net_wrap, "vertical", self.wifi_network_tree.yview)
        net_y.grid(row=0, column=1, sticky="ns")
        self.wifi_network_tree.configure(yscrollcommand=net_y.set)
        
        net_wrap.rowconfigure(0, weight=1)
        net_wrap.columnconfigure(0, weight=1)

        # Right side: Dynamic form area
        make_label(right, "Connect", font=ui_font(13, "bold")).pack(anchor="w", padx=(12, 0))
        self.wifi_form_frame = make_frame(right)
        self.wifi_form_frame.pack(fill=tk.X, pady=(12, 0), padx=(12, 0))

        # ---- Pump Control Section ----
        pump_frame = Section(_scroll_inner, "Pump Control")
        pump_frame.pack(fill=tk.X, pady=(12, 0))

        slider_row = make_frame(pump_frame)
        slider_row.pack(fill=tk.X, padx=10, pady=(8, 8))

        make_label(slider_row, "Speed:").pack(side=tk.LEFT)
        self.pump_pct_var = tk.IntVar(value=0)
        # The slider is the whole control: sliding sends the speed automatically
        # (debounced — see _pump_slider_moved).
        self.pump_slider = ctk.CTkSlider(
            slider_row, from_=0, to=100, number_of_steps=100,
            variable=self.pump_pct_var,
            command=self._pump_slider_moved,
            progress_color=self.c_accent, button_color=self.c_accent,
            button_hover_color=C_ACCENT_HOVER,
        )
        self.pump_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self.pump_label_var = tk.StringVar(value="0%")
        make_label(slider_row, textvariable=self.pump_label_var, width=45).pack(side=tk.LEFT)

        # ---- CH4 Calibration Section ----
        calib_frame = Section(_scroll_inner, "TGS2611 Sensor Calibration")
        calib_frame.pack(fill=tk.X, pady=(8, 0))

        info_row = make_frame(calib_frame)
        info_row.pack(fill=tk.X, padx=10, pady=(8, 2))
        make_label(
            info_row,
            "Enter 1\u20135 calibration points. The GUI fits the Shah model and uploads R\u2082ppm and \u03b1 to the sensor EEPROM.",
            justify=tk.LEFT,
        ).pack(anchor="w")
        make_label(
            info_row,
            "1 point: fixes \u03b1 at Shah default (0.502), solves for R\u2082ppm.  2\u20135 points: fits both.",
            justify=tk.LEFT,
            hint=True,
        ).pack(anchor="w", pady=(2, 0))

        ch_row = make_frame(calib_frame)
        ch_row.pack(fill=tk.X, padx=10, pady=(6, 4))
        make_label(ch_row, "Channel index:").pack(side=tk.LEFT)
        self.calib_ch_var = tk.StringVar(value="1")
        ctk.CTkEntry(ch_row, textvariable=self.calib_ch_var, width=50, font=ui_font(13)).pack(side=tk.LEFT, padx=(6, 0))
        make_label(ch_row, "  (1 for first/only TGS2611)", hint=True).pack(side=tk.LEFT)

        # Dynamic data-point rows
        MAX_CALIB_POINTS = 5
        self.calib_raw_vars: list[tk.StringVar] = []
        self.calib_ppm_vars: list[tk.StringVar] = []
        self._calib_rows_frame = make_frame(calib_frame)
        self._calib_rows_frame.pack(fill=tk.X, padx=10, pady=(2, 0))

        calib_btn_row = make_frame(calib_frame)
        calib_btn_row.pack(fill=tk.X, padx=10, pady=(6, 10))
        self._calib_add_btn = AccentButton(
            calib_btn_row, text="Add row",
            command=self._calib_add_row, state=tk.DISABLED,
        )
        self._calib_add_btn.pack(side=tk.LEFT)
        self.calib_r2ppm_btn = AccentButton(
            calib_btn_row, text="Calculate & Send",
            command=self._calib_multipoint_send, state=tk.DISABLED,
        )
        self.calib_r2ppm_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.calib_r2ppm_status_var = tk.StringVar(value="")
        make_label(calib_btn_row, textvariable=self.calib_r2ppm_status_var, hint=True).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        # Start with one row
        self._calib_add_row()

        # ---- LoRa Section ----
        lora_frame = Section(_scroll_inner, "LoRa")
        lora_frame.pack(fill=tk.X, pady=(8, 4))

        self.lora_dev_eui_var  = tk.StringVar(value="—")
        self.lora_app_eui_var  = tk.StringVar(value="—")
        self.lora_app_key_var  = tk.StringVar(value="—")
        self.lora_status_var   = tk.StringVar(value="")
        self._lora_colon_var   = tk.BooleanVar(value=True)
        self._lora_dev_eui_raw = ""
        self._lora_app_eui_raw = ""

        lora_grid = make_frame(lora_frame)
        lora_grid.pack(fill=tk.X, padx=10, pady=(8, 4))
        lora_grid.columnconfigure(1, weight=1)

        make_label(lora_grid, "Device EUI:", anchor="w").grid(row=0, column=0, sticky="w", pady=2)
        make_readonly_entry(lora_grid, self.lora_dev_eui_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))

        make_label(lora_grid, "App EUI:", anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        make_readonly_entry(lora_grid, self.lora_app_eui_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0))

        make_label(lora_grid, "App Key:", anchor="w").grid(row=2, column=0, sticky="w", pady=2)
        make_readonly_entry(lora_grid, self.lora_app_key_var).grid(
            row=2, column=1, sticky="ew", padx=(8, 0))

        lora_btn_row = make_frame(lora_frame)
        lora_btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.lora_refresh_btn = AccentButton(
            lora_btn_row, text="Refresh", command=self._lora_refresh,
            state=tk.DISABLED,
        )
        self.lora_refresh_btn.pack(side=tk.LEFT)

        self.lora_gen_btn = AccentButton(
            lora_btn_row, text="Generate Keys", command=self._lora_gen_keys,
            state=tk.DISABLED,
        )
        self.lora_gen_btn.pack(side=tk.LEFT, padx=(8, 0))

        ctk.CTkCheckBox(
            lora_btn_row, text="Colon separators", variable=self._lora_colon_var,
            command=self._toggle_eui_colons, font=ui_font(13), text_color=self.c_text,
            checkbox_width=20, checkbox_height=20,
        ).pack(side=tk.LEFT, padx=(8, 0))

        make_label(lora_btn_row, textvariable=self.lora_status_var, hint=True).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        # ---- Advanced Settings (EEPROM Write) ----
        self._eeprom_advanced_btn = AccentButton(
            _scroll_inner, text="\u2699  Advanced Settings",
            command=self._toggle_eeprom_advanced,
        )
        self._eeprom_advanced_btn.pack(anchor="w", pady=(10, 2))

        self._eeprom_frame = Section(_scroll_inner, "TGS EEPROM")
        # packed/hidden by _toggle_eeprom_advanced

        eep_grid = make_frame(self._eeprom_frame)
        eep_grid.pack(fill=tk.X, padx=10, pady=(8, 4))
        eep_grid.columnconfigure(1, weight=1)

        make_label(eep_grid, "Channel index:").grid(row=0, column=0, sticky="w", pady=2)
        self.eeprom_ch_var = tk.StringVar(value="1")
        ctk.CTkEntry(eep_grid, textvariable=self.eeprom_ch_var, width=50, font=ui_font(13)).grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        make_label(eep_grid, "TGS ID:").grid(row=1, column=0, sticky="w", pady=2)
        self.eeprom_tgs_id_var = tk.StringVar(value="")
        ctk.CTkEntry(eep_grid, textvariable=self.eeprom_tgs_id_var, width=120, font=ui_font(13)).grid(
            row=1, column=1, sticky="w", padx=(8, 0))

        make_label(eep_grid, "Lot number:").grid(row=2, column=0, sticky="w", pady=2)
        self.eeprom_lot_var = tk.StringVar(value="")
        ctk.CTkEntry(eep_grid, textvariable=self.eeprom_lot_var, width=200, font=ui_font(13)).grid(
            row=2, column=1, sticky="w", padx=(8, 0))

        make_label(eep_grid, "Board ID:").grid(row=3, column=0, sticky="w", pady=2)
        self.eeprom_board_var = tk.StringVar(value="")
        ctk.CTkEntry(eep_grid, textvariable=self.eeprom_board_var, width=300, font=ui_font(13)).grid(
            row=3, column=1, sticky="w", padx=(8, 0))

        eep_btn_row = make_frame(self._eeprom_frame)
        eep_btn_row.pack(fill=tk.X, padx=10, pady=(4, 8))

        self.eeprom_write_btn = AccentButton(
            eep_btn_row, text="Write", command=self._eeprom_write_ids,
            state=tk.DISABLED,
        )
        self.eeprom_write_btn.pack(side=tk.LEFT)

        self.eeprom_read_btn = AccentButton(
            eep_btn_row, text="Read", command=self._eeprom_read_info,
            state=tk.DISABLED,
        )
        self.eeprom_read_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.eeprom_clear_btn = AccentButton(
            eep_btn_row, text="Erase", command=self._eeprom_clear_clicked,
            state=tk.DISABLED,
        )
        self.eeprom_clear_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.eeprom_status_var = tk.StringVar(value="")
        make_label(eep_btn_row, textvariable=self.eeprom_status_var, hint=True).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        # ---- Mainboard ID (ESP32 NVS) — inside Advanced Settings ----
        self._board_id_frame = Section(_scroll_inner, "Mainboard NVS")
        # packed/hidden by _toggle_eeprom_advanced

        bid_grid = make_frame(self._board_id_frame)
        bid_grid.pack(fill=tk.X, padx=10, pady=(8, 4))
        bid_grid.columnconfigure(1, weight=1)

        make_label(bid_grid, "Board ID:").grid(row=0, column=0, sticky="w", pady=2)
        self.board_id_var = tk.StringVar(value="")
        ctk.CTkEntry(bid_grid, textvariable=self.board_id_var, width=300, font=ui_font(13)).grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        bid_btn_row = make_frame(self._board_id_frame)
        bid_btn_row.pack(fill=tk.X, padx=10, pady=(4, 8))

        self.board_id_write_btn = AccentButton(
            bid_btn_row, text="Write", command=self._board_id_write_clicked,
            state=tk.DISABLED,
        )
        self.board_id_write_btn.pack(side=tk.LEFT)

        self.board_id_read_btn = AccentButton(
            bid_btn_row, text="Read", command=self._board_id_read_clicked,
            state=tk.DISABLED,
        )
        self.board_id_read_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.board_id_clear_btn = AccentButton(
            bid_btn_row, text="Erase", command=self._board_id_clear_clicked,
            state=tk.DISABLED,
        )
        self.board_id_clear_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.board_id_status_var = tk.StringVar(value="")
        make_label(bid_btn_row, textvariable=self.board_id_status_var, hint=True).pack(
            side=tk.LEFT, padx=(12, 0)
        )

        # ---- About (version compatibility) ----
        about_frame = Section(_scroll_inner, "About")
        about_frame.pack(fill=tk.X, pady=(12, 4))

        self.gui_version_var = tk.StringVar(value=GUI_VERSION)
        self.fw_version_var = tk.StringVar(value="\u2014")
        self.version_status_var = tk.StringVar(value="")

        about_grid = make_frame(about_frame)
        about_grid.pack(fill=tk.X, padx=10, pady=(8, 4))
        about_grid.columnconfigure(1, weight=1)

        make_label(about_grid, "GUI version:", anchor="w").grid(
            row=0, column=0, sticky="w", pady=2)
        make_readonly_entry(about_grid, self.gui_version_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0))

        make_label(about_grid, "Firmware version:", anchor="w").grid(
            row=1, column=0, sticky="w", pady=2)
        make_readonly_entry(about_grid, self.fw_version_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0))

        about_btn_row = make_frame(about_frame)
        about_btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.about_refresh_btn = AccentButton(
            about_btn_row, text="Refresh", command=self._about_refresh,
            state=tk.DISABLED,
        )
        self.about_refresh_btn.pack(side=tk.LEFT)

        self.version_status_label = make_label(
            about_btn_row, textvariable=self.version_status_var, hint=True)
        self.version_status_label.pack(side=tk.LEFT, padx=(12, 0))

    def _about_refresh(self) -> None:
        """Read the firmware version from the device and check compatibility."""
        if not self.connected:
            return
        self.about_refresh_btn.configure(state=tk.DISABLED)
        self.fw_version_var.set("\u2026")
        self.version_status_var.set("Reading\u2026")
        self.version_status_label.configure(text_color=C_HINT)

        def worker() -> None:
            try:
                result = self.client.fw_version(timeout_s=5.0)
                if result.get("success"):
                    self.events.put(("fw_version_ok", result["version"]))
                else:
                    self.events.put(("fw_version_failed", result.get("message", "")))
            except Exception as exc:
                self.events.put(("fw_version_failed", str(exc)))
            finally:
                self.events.put(("about_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_fw_version(self, version: str) -> None:
        """Store the reported firmware version and update the compat indicator."""
        self.fw_version_var.set(version or "\u2014")
        _compatible, text, level = version_compat(GUI_VERSION, version)
        self.version_status_var.set(text)
        self.version_status_label.configure(
            text_color=VERSION_LEVEL_COLOURS.get(level, C_HINT))

    def _toggle_eeprom_advanced(self) -> None:
        """Show/hide the EEPROM Write + Mainboard ID sections."""
        visible = self._eeprom_frame.winfo_ismapped() or getattr(self, "_eeprom_visible", False)
        if visible:
            self._eeprom_frame.pack_forget()
            self._board_id_frame.pack_forget()
            self._eeprom_visible = False
        else:
            self._eeprom_frame.pack(fill=tk.X, pady=(0, 4), after=self._eeprom_advanced_btn)
            self._board_id_frame.pack(fill=tk.X, pady=(0, 4), after=self._eeprom_frame)
            self._eeprom_visible = True

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

        make_label(self.wifi_form_frame, f"Saved Network: {ssid}", font=ui_font(13, "bold")).pack(anchor="w", pady=(0, 10))
        
        btn_frame = make_frame(self.wifi_form_frame)
        btn_frame.pack(fill=tk.X)
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        
        if is_connected:
            AccentButton(btn_frame, text="Disconnect", command=self._do_disconnect_from_form).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        else:
            AccentButton(btn_frame, text="Connect", command=self._do_connect_saved).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        AccentButton(btn_frame, text="Forget", command=self._do_forget_saved).grid(row=0, column=1, sticky="ew")

    def _show_psk_network_form(self, ssid: str, security: str) -> None:
        """Show form for a PSK network (password field and Connect/Disconnect button)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "psk"
        self.wifi_current_form_ssid = ssid
        
        is_connected = self.wifi_current_ssid == ssid

        make_label(self.wifi_form_frame, f"Network: {ssid}", font=ui_font(13, "bold")).pack(anchor="w", pady=(0, 10))
        make_label(self.wifi_form_frame, f"Security: {security}", hint=True).pack(anchor="w", pady=(0, 10))

        if not is_connected:
            make_label(self.wifi_form_frame, "Password:").pack(anchor="w")
            self.wifi_form_password = ctk.CTkEntry(self.wifi_form_frame, show="*", font=ui_font(13))
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 12))
            AccentButton(self.wifi_form_frame, text="Connect", command=self._do_connect_psk).pack(fill=tk.X)
        else:
            AccentButton(self.wifi_form_frame, text="Disconnect", command=self._do_disconnect_from_form).pack(fill=tk.X)

    def _show_ent_network_form(self, ssid: str, security: str) -> None:
        """Show form for an ENT network (username, password, and Connect/Disconnect button)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "ent"
        self.wifi_current_form_ssid = ssid
        
        is_connected = self.wifi_current_ssid == ssid

        make_label(self.wifi_form_frame, f"Network: {ssid}", font=ui_font(13, "bold")).pack(anchor="w", pady=(0, 10))
        make_label(self.wifi_form_frame, f"Security: {security}", hint=True).pack(anchor="w", pady=(0, 10))

        if not is_connected:
            make_label(self.wifi_form_frame, "Username:").pack(anchor="w")
            self.wifi_form_username = ctk.CTkEntry(self.wifi_form_frame, font=ui_font(13))
            self.wifi_form_username.pack(fill=tk.X, pady=(4, 8))

            make_label(self.wifi_form_frame, "Password:").pack(anchor="w")
            self.wifi_form_password = ctk.CTkEntry(self.wifi_form_frame, show="*", font=ui_font(13))
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 12))
            AccentButton(self.wifi_form_frame, text="Connect", command=self._do_connect_ent).pack(fill=tk.X)
        else:
            AccentButton(self.wifi_form_frame, text="Disconnect", command=self._do_disconnect_from_form).pack(fill=tk.X)

    def _show_other_network_form(self) -> None:
        """Show form for 'Other' network (SSID, security type, and password/username fields)."""
        self._clear_wifi_form()
        self.wifi_current_form_type = "other"

        make_label(self.wifi_form_frame, "Manual Network Entry", font=ui_font(13, "bold")).pack(anchor="w", pady=(0, 10))

        make_label(self.wifi_form_frame, "Network Name (SSID):").pack(anchor="w")
        self.wifi_form_ssid = ctk.CTkEntry(self.wifi_form_frame, font=ui_font(13))
        self.wifi_form_ssid.pack(fill=tk.X, pady=(4, 8))

        make_label(self.wifi_form_frame, "Security Type:").pack(anchor="w")
        self.wifi_form_security = ctk.CTkComboBox(
            self.wifi_form_frame,
            values=["None", "WPA2-PSK", "WPA2-ENT"],
            state="readonly",
            font=ui_font(13), dropdown_font=ui_font(13),
            command=self._on_other_security_changed,
        )
        self.wifi_form_security.set("")
        self.wifi_form_security.pack(fill=tk.X, pady=(4, 8))

        # Container for dynamic fields
        self.wifi_form_fields_frame = make_frame(self.wifi_form_frame)
        self.wifi_form_fields_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        # Add Connect button at the bottom
        AccentButton(self.wifi_form_frame, text="Connect", command=self._do_connect_other).pack(fill=tk.X)

    def _on_other_security_changed(self, _value=None) -> None:
        """Update form fields when security type changes in 'Other' form."""
        # Clear previous fields
        for widget in self.wifi_form_fields_frame.winfo_children():
            widget.destroy()

        security = self.wifi_form_security.get()

        if security == "None":
            # No fields needed
            pass
        elif security == "WPA2-PSK":
            make_label(self.wifi_form_fields_frame, "Password:").pack(anchor="w")
            self.wifi_form_password = ctk.CTkEntry(self.wifi_form_fields_frame, show="*", font=ui_font(13))
            self.wifi_form_password.pack(fill=tk.X, pady=(4, 0))
        elif security == "WPA2-ENT":
            make_label(self.wifi_form_fields_frame, "Username:").pack(anchor="w")
            self.wifi_form_username = ctk.CTkEntry(self.wifi_form_fields_frame, font=ui_font(13))
            self.wifi_form_username.pack(fill=tk.X, pady=(4, 8))
            make_label(self.wifi_form_fields_frame, "Password:").pack(anchor="w")
            self.wifi_form_password = ctk.CTkEntry(self.wifi_form_fields_frame, show="*", font=ui_font(13))
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
        self.select_all_btn.configure(state=tk.NORMAL if download_ok else tk.DISABLED)
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
                    self.connect_btn.configure(text="✖  Disconnect")
                    self._set_port_combo_state()
                    self.live_start_btn.configure(state=tk.NORMAL)
                    # Grey out file/WiFi buttons during initialization
                    self.refresh_files_btn.configure(state=tk.DISABLED)
                    self.select_all_btn.configure(state=tk.DISABLED)
                    self.download_btn.configure(state=tk.DISABLED)
                    self.wifi_scan_btn.configure(state=tk.DISABLED)
                    self.wifi_saved_btn.configure(state=tk.DISABLED)
                    self.wifi_reset_btn.configure(state=tk.DISABLED)
                    self.refresh_ports()
                    # Start RTC polling
                    self._schedule_rtc_poll()
                    # Fetch the stored pump speed once so the slider starts
                    # from the device's persisted setting.
                    self._initial_status_fetch()
                    # Check firmware/GUI version compatibility
                    self._about_refresh()
                elif kind == "disconnected":
                    self.connected = False
                    self.connected_port = None
                    self.live_running = False
                    self.files_refresh_inflight = False
                    self.preview_loading_index = None
                    self.status_var.set("Disconnected")
                    self.connect_btn.configure(text="🔗  Connect")
                    self._set_port_combo_state()
                    self.live_start_btn.configure(state=tk.DISABLED)
                    self.live_stop_btn.configure(state=tk.DISABLED)
                    # Clear live table and graphs
                    self.live_table.delete(*self.live_table.get_children())
                    self.live_table_columns = []
                    self.live_table_col_indices: list[int] = []
                    self.live_table["columns"] = ()
                    self._reset_live_history()
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
                    self.rtc_current_datetime = "0000-00-00 00:00"
                    self.rtc_time_label.configure(text=self.rtc_current_datetime)
                    # About: firmware version is per-device, so clear it on
                    # disconnect (the GUI version field is a local constant and
                    # stays put).
                    self.fw_version_var.set("\u2014")
                    self.version_status_var.set("")
                    # Pump controls are unavailable while disconnected.
                    self._pump_pending = None
                    self._pump_send_inflight = False
                elif kind == "live_line":
                    self._append_live_line(str(payload))
                elif kind == "files":
                    if self.connected:
                        self._show_files(payload)
                elif kind == "download_ok":
                    path, nbytes = payload
                    self.status_var.set(f"Downloaded: {path}")
                    messagebox.showinfo("Download Complete", f"Saved:\n{path}\n\n{nbytes} bytes")
                elif kind == "batch_download_ok":
                    out_dir, saved = payload
                    total_bytes = sum(nb for _, nb in saved)
                    file_list = "\n".join(os.path.basename(p) for p, _ in saved)
                    self.status_var.set(f"Downloaded {len(saved)} files to {out_dir}")
                    messagebox.showinfo(
                        "Batch Download Complete",
                        f"Saved {len(saved)} files to:\n{out_dir}\n\n{file_list}\n\nTotal: {total_bytes:,} bytes",
                    )
                elif kind == "preview_ok":
                    if not self.connected:
                        continue
                    index, path, headers, rows, nbytes, info, graph_headers, graph_all_rows = payload
                    self.preview_cache[index] = (path, headers, rows, nbytes, info, graph_headers, graph_all_rows)
                    if self._current_selected_index() == index:
                        self._set_preview_table(path, headers, rows, nbytes, info)
                        self._render_log_graphs(graph_headers, graph_all_rows)
                    self.status_var.set(f"Preview loaded: {path}")
                elif kind == "preview_done":
                    if self.preview_loading_index == payload:
                        self.preview_loading_index = None
                elif kind == "live_starting":
                    self._reset_live_history()
                elif kind == "live_started":
                    self.live_running = True
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
                    # Clear table and graphs
                    self.live_table.delete(*self.live_table.get_children())
                    self.live_table_columns = []
                    self.live_table_col_indices: list[int] = []
                    self.live_table["columns"] = ()
                    self._reset_live_history()
                    self._update_wifi_controls()
                    self._update_files_controls()
                elif kind == "initial_status_done":
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
                        self._wifi_disc_strikes = 0
                        self._update_wifi_top_bar()
                        self._schedule_wifi_poll()
                        self.status_var.set(f"Connected to {result['ssid']}")
                        # Fetch status, saved networks, and rescan in a single thread
                        # to avoid overlapping serial commands
                        def post_connect() -> None:
                            try:
                                status = self.client.wifi_get_status(timeout_s=8.0)
                                self.events.put(("wifi_status_ok", status))
                            except Exception:
                                pass
                            try:
                                networks = self.client.wifi_get_saved_networks(timeout_s=10.0)
                                self.events.put(("wifi_saved_ok", networks))
                            except Exception:
                                pass
                        threading.Thread(target=post_connect, daemon=True).start()
                        # Refresh the form to show Disconnect button
                        if self.wifi_network_tree.selection():
                            self._on_network_select()
                    else:
                        msg = result.get("message", "Connection failed")
                        self.status_var.set(f"WiFi: {msg}")
                        messagebox.showwarning("WiFi Connection Failed", msg)
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
                    
                    # Require two consecutive "not connected" readings before
                    # flipping the UI to Disconnected. A single empty reading can
                    # be transient (e.g. the device briefly reports a drop during
                    # a reconnect); reacting to it immediately made the status bar
                    # flash "WiFi: Disconnected" right after connecting.
                    if not new_ssid and self.wifi_current_ssid:
                        self._wifi_disc_strikes = getattr(self, "_wifi_disc_strikes", 0) + 1
                    else:
                        self._wifi_disc_strikes = 0
                    confirmed = bool(new_ssid) or self._wifi_disc_strikes >= 2
                    
                    if confirmed:
                        # Update if values changed
                        ssid_changed = (self.wifi_current_ssid != new_ssid)
                        rssi_changed = (self.wifi_current_rssi != new_rssi)
                        
                        if ssid_changed or rssi_changed:
                            self.wifi_current_ssid = new_ssid
                            self.wifi_current_rssi = new_rssi
                            self._update_wifi_top_bar()
                            if ssid_changed:
                                self.status_var.set(f"WiFi: {new_ssid or 'Disconnected'}")
                                # Refresh the form to show Connect/Disconnect button
                                if self.wifi_network_tree.selection():
                                    self._on_network_select()
                        
                        # Start wifi polling if device is connected and polling isn't running
                        if new_ssid and self.wifi_poll_timer is None:
                            self._schedule_wifi_poll()
                elif kind == "rtc_time_ok":
                    result = payload
                    if result.get("success", False):
                        raw_time = result.get("time", "")
                        # Strip seconds if present (e.g. "2026-04-18 10:04:16" -> "2026-04-18 10:04")
                        parts = raw_time.strip().split()
                        if len(parts) >= 2:
                            tp = parts[1].split(":")
                            if len(tp) >= 2:
                                raw_time = f"{parts[0]} {tp[0]}:{tp[1]}"
                        self.rtc_current_datetime = raw_time
                        self.rtc_time_label.configure(text=self.rtc_current_datetime)
                elif kind == "busy":
                    self.status_var.set(str(payload))
                elif kind == "device_status":
                    msg = str(payload)
                    if msg.startswith("ERROR"):
                        self.status_var.set(f"Device error: {msg}")
                        messagebox.showerror("Device Error", f"The device reported an error at startup:\n\n{msg}")
                    else:
                        self.status_var.set(f"Device: {msg}")
                elif kind == "pump_ok":
                    self.status_var.set(f"Pump speed set to {payload}%")
                elif kind == "pump_status_init":
                    # Initial read after connect: set the slider once from the
                    # device's persisted speed.
                    self._apply_pump_status(payload, sync_slider=True)
                elif kind == "pump_done":
                    # A send finished; push any newer slider value that arrived
                    # while it was in flight.
                    self._pump_send_inflight = False
                    if self._pump_pending is not None and self._pump_debounce_id is None:
                        self._pump_flush()
                elif kind == "calib_r2ppm_ok":
                    ch_disp, r2ppm, alpha, ts_stored = payload
                    note = "" if ts_stored else "  (calib time not stored)"
                    self.calib_r2ppm_status_var.set(
                        f"ch{ch_disp}: R\u2082ppm={r2ppm:.4f} k\u03a9, \u03b1={alpha:.4f} \u2713{note}")
                    self.status_var.set(f"TGS2611 ch{ch_disp} calibrated")
                elif kind == "calib_r2ppm_done":
                    self._update_wifi_controls()
                elif kind == "lora_info_ok":
                    info = payload
                    self._lora_dev_eui_raw = info.get("dev_eui") or "READ_ERROR"
                    self._lora_app_eui_raw = info.get("app_eui") or "Not configured"
                    self.lora_dev_eui_var.set(self._fmt_eui(self._lora_dev_eui_raw))
                    self.lora_app_eui_var.set(self._fmt_eui(self._lora_app_eui_raw))
                    self.lora_app_key_var.set(info.get("app_key") or "Not configured")
                    if info.get("generated"):
                        self.lora_status_var.set("Keys generated ✓")
                    else:
                        self.lora_status_var.set("")
                elif kind == "lora_done":
                    self._update_wifi_controls()
                elif kind == "eeprom_read_done":
                    (result,) = payload
                    self._eeprom_confirm_write(result)
                elif kind == "eeprom_write_ok":
                    ch_disp, tgs_id, lot, board = payload
                    dash = "\u2014"
                    self.eeprom_status_var.set(
                        f"ch{ch_disp}: ID={tgs_id}, Lot={lot or dash}, Board={board or dash} \u2713")
                    self.status_var.set(f"TGS ch{ch_disp} EEPROM written")
                elif kind == "eeprom_write_done":
                    self._update_wifi_controls()
                elif kind == "eeprom_info_ok":
                    ch_disp, result = payload
                    if result.get("success"):
                        messagebox.showinfo(
                            f"EEPROM \u2014 Channel {ch_disp}",
                            f"Currently stored values:\n\n{self._eeprom_values_text(result)}")
                        self.eeprom_status_var.set("Read OK")
                    else:
                        messagebox.showerror(
                            "EEPROM Read Failed",
                            f"Could not read channel {ch_disp}:\n"
                            f"{result.get('message', 'read failed')}")
                        self.eeprom_status_var.set("Read FAILED")
                elif kind == "eeprom_read_btn_done":
                    self._update_wifi_controls()
                elif kind == "eeprom_clear_confirm":
                    ch_disp, result = payload
                    if result.get("success"):
                        current = self._eeprom_values_text(result)
                    else:
                        current = ("(could not read current values \u2014 "
                                   f"{result.get('message', 'read failed')})")
                    if messagebox.askyesno(
                            "Confirm EEPROM Clear",
                            f"Channel {ch_disp} \u2014 values currently stored:\n\n"
                            f"{current}\n\n"
                            f"This will erase the TGS ID, the calibration coefficients\n"
                            f"(R\u2082ppm/\u03b1), the calibration date, Lot # and Board ID\n"
                            f"from the sensor EEPROM. This cannot be undone.\n\n"
                            f"Are you sure you want to continue?",
                            icon="warning"):
                        self._eeprom_do_clear(ch_disp)
                    else:
                        self.eeprom_status_var.set("Cancelled")
                        self._update_wifi_controls()
                elif kind == "eeprom_clear_ok":
                    ch_disp = payload
                    self.eeprom_status_var.set(f"ch{ch_disp}: EEPROM cleared \u2713")
                    self.status_var.set(f"TGS ch{ch_disp} EEPROM erased")
                elif kind == "eeprom_clear_btn_done":
                    self._update_wifi_controls()
                elif kind == "board_id_write_ok":
                    bid = payload
                    self.board_id_status_var.set(f"Board ID '{bid}' stored \u2713")
                    self.status_var.set("Mainboard ID written to ESP32 memory")
                elif kind == "board_id_read_ok":
                    result = payload
                    bid = result.get("board_id") or ""
                    messagebox.showinfo(
                        "Mainboard ID",
                        f"Currently stored values:\n\n"
                        f"  Board ID: {bid or '(not set)'}")
                    self.board_id_status_var.set(
                        f"Board ID: {bid or '(not set)'} \u2713")
                elif kind == "board_id_clear_ok":
                    self.board_id_status_var.set("Board ID erased \u2713")
                    self.status_var.set("Mainboard ID erased from ESP32 memory")
                elif kind == "board_id_btn_done":
                    self._update_wifi_controls()
                elif kind == "fw_version_ok":
                    self._apply_fw_version(str(payload))
                elif kind == "fw_version_failed":
                    # The About verdict stays one of the four fixed forms; the
                    # reason for the failed read goes to the main status line so
                    # the diagnostic is not lost.
                    self.fw_version_var.set("\u2014")
                    self.version_status_var.set(VERSION_UNKNOWN_TEXT)
                    self.version_status_label.configure(
                        text_color=VERSION_LEVEL_COLOURS[VERSION_LEVEL_UNKNOWN])
                    msg = str(payload or "")
                    if "code 1" in msg or "Timeout" in msg:
                        self.status_var.set(
                            "Firmware did not answer the version query "
                            "(too old to support About — re-flash to enable it)")
                    else:
                        self.status_var.set(f"Firmware version read failed: {msg}")
                elif kind == "about_done":
                    self._update_wifi_controls()
                elif kind == "mqtt_status":
                    self._handle_mqtt_status(str(payload))
                elif kind == "mqtt_message":
                    device_id, row = payload
                    self._handle_mqtt_message(device_id, row)
                elif kind == "mqtt_error":
                    self.mqtt_status_var.set(f"MQTT error: {payload}")
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
        self.port_combo.configure(values=displays)

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

    def _initial_status_fetch(self) -> None:
        """Fetch WiFi status and the stored pump speed after COM connect."""
        def worker() -> None:
            # First, drain any unsolicited STATUS messages (e.g. boot errors like missing mux)
            msgs = self.client.drain_status_messages(listen_s=1.5)
            for m in msgs:
                self.events.put(("device_status", m))
            try:
                status = self.client.wifi_get_status(timeout_s=5.0)
                self.events.put(("wifi_status_ok", status))
            except Exception:
                pass
            try:
                pump = self.client.pump_get(timeout_s=3.0)
                if pump.get("success"):
                    self.events.put(("pump_status_init", pump))
            except Exception:
                pass
            try:
                lora = self.client.lora_info(timeout_s=10.0)
                if lora.get("success"):
                    self.events.put(("lora_info_ok", lora))
            except Exception:
                pass
            # Firmware version for Settings > About (compatibility check).
            try:
                ver = self.client.fw_version(timeout_s=5.0)
                if ver.get("success"):
                    self.events.put(("fw_version_ok", ver["version"]))
                else:
                    self.events.put(("fw_version_failed", ver.get("message", "")))
            except Exception as exc:
                self.events.put(("fw_version_failed", str(exc)))
            self.events.put(("initial_status_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def start_live(self) -> None:
        def on_line(line: str) -> None:
            self.events.put(("live_line", line))

        def on_status(message: str) -> None:
            self.events.put(("device_status", message))

        def worker() -> None:
            try:
                self.events.put(("busy", "Starting live stream..."))
                # Reset the display state before the reader can enqueue the
                # firmware header and first data rows.
                self.events.put(("live_starting", None))
                self.client.start_live(on_line, on_status)
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
    
    _LOG_DATE_RE = re.compile(r"(\d{8})")

    @staticmethod
    def _date_str_from_dev_path(dev_path: str) -> str:
        stem = os.path.splitext(os.path.basename(dev_path))[0]
        m = App._LOG_DATE_RE.search(stem)
        if m:
            return m.group(1)
        return datetime.now().strftime("%Y%m%d")

    @staticmethod
    def _drop_csv_comment_rows(parsed: list[list[str]]) -> list[list[str]]:
        """Remove '#' prefixed comment lines from parsed CSV rows.

        The firmware writes a '#META,...' metadata line above the heading of
        every log file; without this it would be mistaken for the heading row
        and break the preview/graph column matching.
        """
        return [r for r in parsed if not (r and r[0].lstrip().startswith("#"))]

    @staticmethod
    def _csv_meta_summary(parsed: list[list[str]]) -> str:
        """Human-readable summary of '#'-prefixed metadata comment rows.

        The firmware writes '#META,key=value,...' as the first line of every log
        file. The key/value pairs are shown in the preview's info line so the
        metadata (board ID, TGS identity, calibration date) is visible without
        downloading the file. Returns "" when the file has no metadata line.
        """
        pairs: list[str] = []
        for row in parsed:
            if not (row and row[0].lstrip().startswith("#")):
                continue
            for cell in row[1:]:
                cell = cell.strip()
                if cell and "=" in cell:
                    pairs.append(cell)
        return " | ".join(pairs)

    @staticmethod
    def _make_csv_excel_safe(raw_bytes: bytes) -> bytes:
        import re
        text = raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")
        reader = csv.reader(io.StringIO(text))
        parsed = list(reader)
        # Firmware writes '#'-prefixed metadata lines (e.g. '#META,...') above
        # the heading. Keep them verbatim in the export; only the heading row
        # and below are treated as table data for the timestamp fix.
        body_start = 0
        while (body_start < len(parsed) and parsed[body_start]
               and parsed[body_start][0].lstrip().startswith("#")):
            body_start += 1
        if body_start >= len(parsed):
            return raw_bytes
        header = parsed[body_start]
        try:
            ts_col = header.index("timestamp")
        except ValueError:
            return raw_bytes
        ts_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        for row in parsed[body_start + 1:]:
            if ts_col < len(row) and ts_pattern.match(row[ts_col].strip()):
                row[ts_col] = " " + row[ts_col].strip()
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerows(parsed)
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    @staticmethod
    def _render_preview_table(
        payload: bytes,
        head_rows: int = 40,
        tail_rows: int = 40,
        max_cols: int = 18,
    ) -> tuple[list[str], list[list[str]], str]:
        text = payload.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        parsed_all = list(reader)
        meta_summary = App._csv_meta_summary(parsed_all)
        parsed = App._drop_csv_comment_rows(parsed_all)
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
        if meta_summary:
            info_parts.append(f"Meta: {meta_summary}")
        else:
            # No comment row at all: files written before metadata logging (or
            # with the line stripped) — say so instead of showing nothing.
            info_parts.append("Meta: (no #META line in this file)")
        if show_split:
            info_parts.append(f"showing first {head_rows} and last {tail_rows} data rows")
        if extra_cols:
            info_parts.append(f"showing first {col_n} columns")
        info = " | ".join(info_parts)
        return headers, rows, info

    @staticmethod
    def _parse_all_for_graphs(payload: bytes) -> tuple[list[str], list[list[str]]]:
        text = payload.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        parsed = App._drop_csv_comment_rows(list(reader))
        if len(parsed) < 2:
            return [], []
        header = [h.strip() for h in parsed[0]]
        return header, parsed[1:]

    def _set_preview_info(self, info: str) -> None:
        """Show/hide the metadata (info) line under the preview title.

        The line is packed on demand and placed before the expanding pane so it
        is always visible: when it carried no text it used to sit after the
        preview table and was clipped away in a short pane.
        """
        self.preview_info_var.set(info)
        if info:
            if not self.preview_info_label.winfo_manager():
                self.preview_info_label.pack(anchor="w", fill="x", pady=(2, 0),
                                             before=self.inner_split)
        else:
            self.preview_info_label.pack_forget()

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
        self._set_preview_info("")

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

        # Filter to only show timestamp + gas columns
        gas_col_names = set(self.GAS_COLS.keys()) | {"timestamp", "tgs2611_1_raw_avg"}
        keep_indices = [i for i, h in enumerate(headers) if h in gas_col_names]
        display_headers = [headers[i] for i in keep_indices]
        display_rows = [[row[i] if i < len(row) else "" for i in keep_indices] for row in rows]

        col_ids = tuple(f"c{i}" for i in range(len(display_headers)))
        self.preview_tree["columns"] = col_ids
        for i, name in enumerate(display_headers):
            cid = col_ids[i]
            self.preview_tree.heading(cid, text=name)
            if name == "timestamp":
                self.preview_tree.column(cid, width=120, anchor="w")
            else:
                self.preview_tree.column(cid, width=80, anchor="w")

        striped_rows = [tuple(row) for row in display_rows]
        self._apply_tree_stripes(self.preview_tree, striped_rows)

        self._set_preview_info(info)

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
            path, headers, rows, nbytes, info, graph_headers, graph_all_rows = cached
            self._set_preview_table(path, headers, rows, nbytes, info)
            self._render_log_graphs(graph_headers, graph_all_rows)
            return

        self._set_preview_message(dev_path, "Loading preview...", 0)
        self.preview_loading_index = idx

        def worker() -> None:
            try:
                self.events.put(("busy", f"Loading preview for {dev_path} ..."))
                path, payload = self.client.download_log_bytes(index=idx, timeout_s=20.0)
                headers, rows, info = self._render_preview_table(
                    payload,
                    head_rows=10000,
                    tail_rows=10000,
                    max_cols=18,
                )
                graph_headers, graph_all_rows = self._parse_all_for_graphs(payload)
                self.events.put(("preview_ok", (idx, path, headers, rows, len(payload), info, graph_headers, graph_all_rows)))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("preview_done", idx))

        threading.Thread(target=worker, daemon=True).start()

    def _select_all_files(self) -> None:
        children = self.files_tree.get_children()
        if children:
            self.files_tree.selection_set(children)

    def _ask_sensor_number(self) -> None:

        dialog = ctk.CTkToplevel(self)
        dialog.title("Sensor Number")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.after(150, dialog.grab_set)

        result: list[str | None] = [None]

        make_label(dialog, "Enter sensor number:").pack(padx=20, pady=(16, 4))
        var = tk.StringVar()

        vcmd = dialog.register(lambda P: P == "" or (P.isdigit() and len(P) <= 3))
        entry = ctk.CTkEntry(dialog, textvariable=var, width=100, validate="key",
                             validatecommand=(vcmd, "%P"), font=ui_font(13))
        entry.pack(padx=20)
        dialog.after(200, entry.focus_set)

        def on_ok() -> None:
            val = var.get().strip()
            if not val.isdigit() or not val:
                messagebox.showwarning("Invalid", "Please enter a valid whole number.", parent=dialog)
                return
            result[0] = f"{int(val):03d}"
            dialog.destroy()

        def on_cancel() -> None:
            dialog.destroy()

        btn_frame = make_frame(dialog)
        btn_frame.pack(pady=(8, 16))
        AccentButton(btn_frame, text="OK", command=on_ok, width=80).pack(side=tk.LEFT)
        AccentButton(btn_frame, text="Cancel", command=on_cancel, width=80).pack(side=tk.LEFT, padx=(8, 0))

        entry.bind("<Return>", lambda _: on_ok())
        dialog.bind("<Escape>", lambda _: on_cancel())

        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{x}+{y}")

        self.wait_window(dialog)
        return result[0]

    def download_selected(self) -> None:
        sel = self.files_tree.selection()
        if not sel:
            messagebox.showwarning("Select File", "Select a file to download.")
            return

        sensor_num = self._ask_sensor_number()
        if sensor_num is None:
            return

        if len(sel) == 1:
            row = self.files_tree.item(sel[0], "values")
            index = int(row[0])
            dev_path = str(row[1])
            date_str = App._date_str_from_dev_path(dev_path)
            default_name = f"{sensor_num}_{date_str}.csv"

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
                    _, payload = self.client.download_log_bytes(index=index, timeout_s=20.0)
                    safe = App._make_csv_excel_safe(payload)
                    out_path.write_bytes(safe)
                    nbytes = len(safe)

                    self.events.put(("download_ok", (str(out_path), nbytes)))
                except Exception as exc:
                    self.events.put(("error", exc))

            threading.Thread(target=worker, daemon=True).start()

        else:
            items = [
                (int(self.files_tree.item(iid, "values")[0]), str(self.files_tree.item(iid, "values")[1]))
                for iid in sel
            ]
            out_dir = filedialog.askdirectory(title=f"Save {len(items)} CSV Files To Folder")
            if not out_dir:
                return
            out_dir_path = Path(out_dir)

            def batch_worker() -> None:
                saved: list[tuple[str, int]] = []
                try:
                    for i, (index, dev_path) in enumerate(items):
                        date_str = App._date_str_from_dev_path(dev_path)
                        name = f"{sensor_num}_{date_str}.csv"
                        out_path = out_dir_path / name
                        self.events.put(("busy", f"Downloading {dev_path} ({len(saved) + 1}/{len(items)}) ..."))
                        _, payload = self.client.download_log_bytes(index=index, timeout_s=20.0)
                        safe = App._make_csv_excel_safe(payload)
                        out_path.write_bytes(safe)
                        nbytes = len(safe)

                        saved.append((str(out_path), nbytes))
                    self.events.put(("batch_download_ok", (str(out_dir_path), saved)))
                except Exception as exc:
                    self.events.put(("error", exc))

            threading.Thread(target=batch_worker, daemon=True).start()

    def _refresh_live_graphs(self) -> None:
        if len(self.live_headers) <= 1 and self.live_series:
            self.live_headers = ["timestamp"] + [f"col_{i}" for i in range(1, len(self.live_series) + 1)]
        
        graph_cols = list(self.GAS_COLS.keys())

        var_names = [
            col for col in graph_cols
            if col in self.live_headers[1:]
        ]

        if var_names != self.graph_names:
            self._rebuild_graph_widgets(var_names)
        
        # Schedule a deferred graph redraw so we don't block event processing.
        # Multiple rapid calls coalesce into a single redraw.
        if self.graph_canvases and not self._graph_redraw_pending:
            self._graph_redraw_pending = True
            self.after_idle(self._deferred_redraw_graphs)

    def _deferred_redraw_graphs(self) -> None:
        """Called via after_idle so the canvas has valid dimensions."""
        self._graph_redraw_pending = False
        self._redraw_graphs()

    def _rebuild_graph_widgets(self, var_names: list[str]) -> None:
        """Rebuild graph canvases for the given variable names."""
        if not hasattr(self, 'graphs_content'):
            return

        for child in self.graphs_content.winfo_children():
            child.destroy()
        self.graph_names = list(var_names)
        self.graph_canvases = []
        self.graph_index_map = []
            
        self.graphs_content.columnconfigure(0, weight=1)
        self.graphs_content.columnconfigure(1, weight=1)

        for i, name in enumerate(var_names):
            row, col = divmod(i, 2)
            self.graphs_content.rowconfigure(row, weight=1)

            graph_title, unit = self.GAS_COLS.get(name, (name, ""))
            pane = Section(self.graphs_content, f"{graph_title} ({unit})" if unit else graph_title)
            pane.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)

            canvas = self._make_graph_canvas(pane, height=220)
            canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 8))
            self.graph_canvases.append(canvas)
            self.graph_index_map.append(name)

    def _on_graph_hover(self, event: tk.Event, canvas: tk.Canvas) -> None:
        """Show tooltip with data values at the hovered point."""

        canvas.delete("hover")
        data = self.graph_hover_data.get(canvas)
        if not data:
            return
        x0, y0, x1, y1 = data["x0"], data["y0"], data["x1"], data["y1"]
        mx = event.x
        if mx < x0 or mx > x1:
            return
        
        xs, ys, ts = data["xs"], data["ys"], data["ts"]
        x_min, x_max = data["x_min"], data["x_max"]
        y_min, y_max = data["y_min"], data["y_max"]

        frac = (mx - x0) / (x1 - x0)
        target_x = x_min + frac * (x_max - x_min)
        closest_idx = min(range(len(xs)), key=lambda i: abs(xs[i] - target_x))
        yv = ys[closest_idx]
        if yv is None:
            return

        px = x0 + (xs[closest_idx] - x_min) * (x1 - x0) / (x_max - x_min)
        py = y1 - (yv - y_min) * (y1 - y0) / (y_max - y_min)

        canvas.create_line(px, y0, px, y1, fill="#aaaaaa", dash=(4, 2), tags="hover")
        r = 4
        canvas.create_oval(px - r, py - r, px + r, py + r, fill="#1f77b4", outline="", tags="hover")

        label = f"{self._fmt_y(yv)} at {ts[closest_idx]}"
        tx = px + 8 if px + 8 + 120 < x1 else px - 8
        anchor = "sw" if tx > px else "se"
        ty = max(py - 6, y0 + 10)
        t = canvas.create_text(tx, ty, text=label, anchor=anchor, fill="#333333", tags="hover")
        bx0, by0, bx1, by1 = canvas.bbox(t)
        canvas.create_rectangle(bx0 - 4, by0 - 2, bx1 + 4, by1 + 2, fill="#f8f8f8", outline="#cccccc", tags="hover")
        canvas.tag_raise(t)

    def _redraw_graphs(self) -> None:
        """Redraw all graphs with current data."""
        if not self.graph_canvases or not self.live_x_values:
            return

        for graph_idx, canvas in enumerate(self.graph_canvases):
            col_name = self.graph_index_map[graph_idx]

            try:
                data_idx = self.live_headers.index(col_name)
            except ValueError:
                continue

            if data_idx - 1 >= len(self.live_series):
                continue

            y_vals = list(self.live_series[data_idx - 1])  # -1 because live_headers includes timestamp at index 0

            self._draw_series(
                canvas,
                list(self.live_x_values),
                list(self.live_time_labels),
                y_vals,
            )

    @staticmethod
    def _fmt_y(v: float) -> str:
        av = abs(v)
        if av >= 1000:
            return f"{v:,.0f}"
        elif av >= 10:
            return f"{v:.1f}"
        elif av >= 1:
            return f"{v:.2f}"
        else:
            return f"{v:.3f}"

    def _draw_series(
        self,
        canvas: tk.Canvas,
        x_values: list[float],
        time_labels: list[str],
        y_values: list[float | None],
    ) -> None:
        """Draw a single data series on the canvas."""
        canvas.delete("all")
        # Skip drawing if canvas hasn't been laid out yet
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        w = cw
        h = ch
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
        mid = (y_min + y_max) / 2
        span = y_max - y_min
        min_span = max(abs(mid) * 0.1, 1.0) # Ensure some minimum vertical scale for better visibility
        if span < min_span:
            y_min = mid - min_span / 2
            y_max = mid + min_span / 2
        padding = (y_max - y_min) * 0.05
        y_min -= padding
        y_max += padding

        def map_x(xv: float) -> float:
            return x0 + (xv - x_min) * (x1 - x0) / (x_max - x_min)

        def map_y(yv: float) -> float:
            return y1 - (yv - y_min) * (y1 - y0) / (y_max - y_min)

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

        canvas.create_text(x0 - 4, y0, text=self._fmt_y(y_max), anchor="ne", fill="#555555")
        canvas.create_text(x0 - 4, y1, text=self._fmt_y(y_min), anchor="se", fill="#555555")
        canvas.create_text(x0, y1 + 14, text=ts[0], anchor="w", fill="#555555")
        canvas.create_text(x1, y1 + 14, text=ts[-1], anchor="e", fill="#555555")

        self.graph_hover_data[canvas] ={
            "xs": xs, "ys": ys, "ts": ts, 
            "x0": x0, "y0": y0, "x1": x1, "y1": y1,
            "x_min": x_min, "x_max": x_max, 
            "y_min": y_min, "y_max": y_max,
        }

    def _reset_live_history(self) -> None:
        self.live_sample_counter = 0
        self.live_headers = ["timestamp"]
        self.live_x_values.clear()
        self.live_time_labels.clear()
        self.live_series = []
        self.live_last_values.clear()
        self.gas_card_stats.clear()
        self.gas_card_last_update.clear()

        for col_name, (val_lbl, unit_lbl, card, stats_lbl) in self.gas_card_widgets.items():
            val_lbl.configure(text="N/A", text_color=self.C_CARD_NA)
            unit_lbl.configure(text_color="#9ca3af")
            card.configure(border_color=self.C_CARD_BORDER_NA)
            stats_lbl.clear()
            self.gas_card_age_labels[col_name].configure(text="No data yet", text_color="#9ca3af")

        self._refresh_live_graphs()

    def _update_gas_cards(self, fields: list[str]) -> None:
        if not self.live_headers or len(self.live_headers) < 2:
            return
        row = {self.live_headers[i]: fields[i] for i in range(min(len(self.live_headers), len(fields)))}

        for col_name, (label, unit) in self.GAS_COLS.items():
            if col_name not in self.gas_card_widgets:
                continue
            val_lbl, unit_lbl, card, stats_lbl = self.gas_card_widgets[col_name]
            raw = row.get(col_name, "NA").strip()
            parsed = self._parse_float_or_none(raw)

            if parsed is None:
                val_lbl.configure(text="NA", text_color=self.C_CARD_NA)
                unit_lbl.configure(text_color="#9ca3af")
                card.configure(border_color=self.C_CARD_BORDER_NA)
            else:
                if unit in ("\u00b0C", "%RH"):
                    display = f"{parsed:.1f}"
                elif parsed == int(parsed) and parsed >= 10:
                    display = str(int(parsed))
                else:
                    display = f"{parsed:.2f}"
                if col_name not in self.gas_card_stats:
                    self.gas_card_stats[col_name] = []
                self.gas_card_stats[col_name].append(parsed)
                s = self.gas_card_stats[col_name]
                stats_lbl.set_stats(f"{min(s):.1f}", f"{sum(s)/len(s):.1f}", f"{max(s):.1f}")

                text_col = self.C_CARD_NEUTRAL
                border_col = self.C_CARD_BORDER_NEUTRAL

                val_lbl.configure(text=display, text_color=text_col)
                unit_lbl.configure(text_color=text_col)
                card.configure(border_color=border_col)

                self.gas_card_last_update[col_name] = time.monotonic()
                self.gas_card_age_labels[col_name].configure(text="Updated just now", text_color="#9ca3af")

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 2:
            return "just now"
        if seconds < 60:
            return f"{int(seconds)}s ago"
        if seconds < 3600:
            return f"{int(seconds // 60)}m {int(seconds % 60)}s ago"
        return f"{int(seconds // 3600)}h {int(seconds % 3600 // 60)}m ago"

    def _tick_gas_card_ages(self) -> None:
        """Refresh each gas card's "Updated … ago" text and grey out stale cards."""
        now = time.monotonic()
        for col_name, last in self.gas_card_last_update.items():
            val_lbl, unit_lbl, card, _stats_lbl = self.gas_card_widgets[col_name]
            age = now - last
            age_lbl = self.gas_card_age_labels[col_name]
            if age >= self.GAS_CARD_STALE_S:
                age_lbl.configure(text=f"updated {self._format_age(age)}", text_color="#b45309")
                val_lbl.configure(text_color=self.C_CARD_NA)
                unit_lbl.configure(text_color="#9ca3af")
                card.configure(border_color=self.C_CARD_BORDER_NA)
            else:
                age_lbl.configure(text=f"Updated {self._format_age(age)}", text_color="#9ca3af")
        self.after(1000, self._tick_gas_card_ages)

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
        if t.lower().endswith("ppm"):
            t = t[:-3].strip()
        try:
            return float(t)
        except ValueError:
            return None

    @staticmethod
    def _is_n2o_column(col_name: str) -> bool:
        return "n2o" in col_name.strip().lower()

    def _apply_live_value_hold(self, fields: list[str]) -> list[str]:
        if len(fields) <= 1 or len(self.live_headers) <= 1:
            return fields

        now = time.time()
        out = list(fields)
        n_vars = min(len(out) - 1, len(self.live_headers) - 1)

        for i in range(n_vars):
            col_name = self.live_headers[i + 1]
            raw = out[i + 1]
            parsed = self._parse_float_or_none(raw)

            if parsed is not None:
                self.live_last_values[col_name] = (parsed, now)
                continue

            # N2O arrives slower than the commit cadence; hold last valid value
            # briefly so a sparse sample doesn't appear as repeated NA in GUI.
            if raw.strip().upper() == "NA" and self._is_n2o_column(col_name):
                last = self.live_last_values.get(col_name)
                if last is not None and (now - last[1]) <= self.live_hold_timeout_s:
                    out[i + 1] = f"{last[0]:.2f}"

        return out

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

    @staticmethod
    def _is_valid_timestamp(ts: str) -> bool:
        """Return True if ts looks like a firmware-generated timestamp.

        The firmware emits one of two forms:
          - "YYYY-MM-DD HH:MM:SS"  (exactly 19 chars, dashes at [4]/[7], space at [10])
          - "RTC NA (...)"         (fallback when the RTC hasn't synced yet)

        A framing-slipped partial payload always puts a truncated time string
        like "2:30:00" in field[0]; those are caught here and discarded.
        """
        s = ts.strip()
        if len(s) == 19 and s[4] == '-' and s[7] == '-' and s[10] == ' ':
            return True
        if s.startswith("RTC"):
            return True
        return False

    def _append_live_line(self, line: str) -> None:
        # Check for header line: first field is "timestamp"
        if line.startswith("timestamp"):
            # Parse header CSV
            hdr = [field.strip() for field in line.split(",")]
            hdr = [field for field in hdr if field]
            # Require at least 2 columns (timestamp + at least one data field).
            # A single-element result means a corrupted/partial header — ignore it
            # to avoid wiping the table with a bogus column configuration.
            if len(hdr) < 2:
                return
            self.live_headers = hdr
            self.live_last_values.clear()
            display_hdr = [col for col in hdr if col not in self.HIDDEN_LOG_COLS]
            display_indices = [i for i, col in enumerate(hdr) if col not in self.HIDDEN_LOG_COLS]
            if display_hdr != self.live_table_columns:
                self.live_table_col_indices = display_indices
                self.live_table_columns = display_hdr
                self.live_table.configure(columns=display_hdr)
                for col in display_hdr:
                    self.live_table.heading(col, text=col)
                    if col == "timestamp":
                        self.live_table.column(col, width=120, anchor="w")
                    else:
                        self.live_table.column(col, width=80, anchor="w")
            return

        # Skip data rows if columns not yet set up
        if not self.live_table_columns:
            return
        
        # Add data row to table
        fields = line.split(",")

        if not self._is_valid_timestamp(fields[0] if fields else ""):
            while len(fields) < len(self.live_headers):
                fields.append("NA")
            fields = ["NA" if f.strip() == "" else f for f in fields]
        else:
            while len(fields) < len(self.live_headers):
                fields.append("")
        fields = self._apply_live_value_hold(fields)

        display_fields = [fields[i] for i in self.live_table_col_indices if i < len(fields)]
        row_id = self.live_table.insert("", tk.END, values=display_fields)

        # Keep table size reasonable (max 2000 rows)
        all_items = self.live_table.get_children()
        if len(all_items) > 2000:
            # Delete oldest 500 rows
            for item in all_items[:500]:
                self.live_table.delete(item)
        
        # Auto-scroll to bottom
        if self.live_autoscroll.get():
            self.live_table.see(row_id)

        # Try to extract and update RTC time from first field (timestamp)
        if fields and len(fields) > 0:
            self._update_rtc_from_timestamp(fields[0])

        self._update_live_history(fields)
        self._update_gas_cards(fields)
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

    def _update_wifi_controls(self) -> None:
        """Enable/disable WiFi, pump and device controls by connection/live state."""
        # Disable WiFi controls during live streaming to avoid interference
        wifi_ok = self.connected and not self.live_running
        self.wifi_scan_btn.configure(state=tk.NORMAL if wifi_ok and not self.wifi_scan_inflight else tk.DISABLED)
        self.wifi_saved_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)
        self.wifi_reset_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)
        # Pump speed slider — the only pump control.
        pump_ok = self.connected and not self.live_running
        pump_st = tk.NORMAL if pump_ok else tk.DISABLED
        self.pump_slider.configure(state=pump_st)
        # CH4 calibration
        self.calib_r2ppm_btn.configure(state=pump_st)
        MAX_CALIB_POINTS = 5
        add_ok = pump_ok and len(self.calib_raw_vars) < MAX_CALIB_POINTS
        self._calib_add_btn.configure(state=tk.NORMAL if add_ok else tk.DISABLED)
        # LoRa
        lora_st = tk.NORMAL if pump_ok else tk.DISABLED
        self.lora_refresh_btn.configure(state=lora_st)
        self.lora_gen_btn.configure(state=lora_st)
        # EEPROM Write section
        self.eeprom_write_btn.configure(state=pump_st)
        self.eeprom_read_btn.configure(state=pump_st)
        self.eeprom_clear_btn.configure(state=pump_st)
        # Mainboard ID (ESP32 NVS) section
        self.board_id_write_btn.configure(state=pump_st)
        self.board_id_read_btn.configure(state=pump_st)
        self.board_id_clear_btn.configure(state=pump_st)
        # About (firmware version query / compatibility check)
        self.about_refresh_btn.configure(state=pump_st)

    def _pump_slider_moved(self, value: str) -> None:
        """Slider callback: update the readout and send the new speed.

        The slider is the only pump control (no Set/Off buttons), so every
        change is pushed automatically. ``_pump_syncing`` suppresses the echo
        when the slider is refreshed from a device reading.
        """
        pct = int(float(value))
        self.pump_label_var.set(f"{pct}%")
        if self._pump_syncing or not self.connected or self.live_running:
            return
        self._pump_pending = pct
        if self._pump_debounce_id is not None:
            self.after_cancel(self._pump_debounce_id)
        self._pump_debounce_id = self.after(PUMP_SEND_DEBOUNCE_MS, self._pump_flush)

    def _pump_flush(self) -> None:
        """Send the latest slider value once the user pauses (debounce timer)."""
        self._pump_debounce_id = None
        if self.live_running:
            # Live streaming owns the serial port; drop the queued value rather
            # than race its reader (the slider is disabled while streaming).
            self._pump_pending = None
            return
        if self._pump_pending is None or self._pump_send_inflight:
            return  # an in-flight send picks the newest value up when it finishes
        pct = self._pump_pending
        self._pump_pending = None
        self._pump_send(pct)

    def _pump_send(self, pct: int) -> None:
        """Send a pump speed command in a background thread."""
        self._pump_send_inflight = True

        def worker() -> None:
            try:
                result = self.client.pump_set(pct, timeout_s=3.0)
                if result.get("success"):
                    self.events.put(("pump_ok", pct))
                else:
                    self.events.put(("error", result.get("message", "Pump set failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("pump_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_pump_status(self, status: dict, sync_slider: bool = False) -> None:
        """Apply the device's stored pump speed to the slider when requested."""
        pct = int(status.get("percent", 0))
        if sync_slider and self._pump_pending is None and not self._pump_send_inflight:
            self._pump_syncing = True
            try:
                self.pump_pct_var.set(pct)
                self.pump_label_var.set(f"{pct}%")
            finally:
                self._pump_syncing = False

    def _calib_add_row(self) -> None:
        """Append a new (ppm, raw ADC) entry row to the calibration panel."""
        MAX_CALIB_POINTS = 5
        if len(self.calib_raw_vars) >= MAX_CALIB_POINTS:
            return
        idx = len(self.calib_raw_vars)
        ppm_var = tk.StringVar(value="")
        raw_var = tk.StringVar(value="")
        self.calib_ppm_vars.append(ppm_var)
        self.calib_raw_vars.append(raw_var)

        row = make_frame(self._calib_rows_frame)
        row.pack(fill=tk.X, pady=2)
        make_label(row, "CH\u2084 standard (ppm):").pack(side=tk.LEFT)
        ctk.CTkEntry(row, textvariable=ppm_var, width=80, font=ui_font(13)).pack(side=tk.LEFT, padx=(6, 0))
        make_label(row, "  Measured value (raw):").pack(side=tk.LEFT, padx=(10, 0))
        ctk.CTkEntry(row, textvariable=raw_var, width=80, font=ui_font(13)).pack(side=tk.LEFT, padx=(6, 0))

        # Disable Add row when at max
        if len(self.calib_raw_vars) >= MAX_CALIB_POINTS:
            self._calib_add_btn.configure(state=tk.DISABLED)

    def _calib_multipoint_send(self) -> None:
        """Fit Shah model from entered data points, then upload R2ppm and alpha to device."""
        # --- Parse channel ---
        try:
            ch_display = int(self.calib_ch_var.get())
            if ch_display < 1:
                raise ValueError
            ch = ch_display - 1  # firmware is 0-based
        except ValueError:
            self.calib_r2ppm_status_var.set("Invalid channel (must be \u2265 1)")
            return

        # --- Parse data points (skip empty rows) ---
        ADS_LSB_V = 62.5e-6
        VS_V = 5.0
        R_L_KOHM = 1.0
        SHAH_A = 24.7
        SHAH_ALPHA_DEFAULT = 0.502

        points: list[tuple[float, float]] = []  # (Rs_kohm, ppm)
        for i in range(len(self.calib_raw_vars)):
            raw_str = self.calib_raw_vars[i].get().strip()
            ppm_str = self.calib_ppm_vars[i].get().strip()
            if not raw_str and not ppm_str:
                continue  # empty row, skip
            if not raw_str or not ppm_str:
                self.calib_r2ppm_status_var.set(f"Row {i+1}: fill both ADC and ppm, or leave both empty")
                return
            try:
                raw = int(raw_str)
                ppm = float(ppm_str)
            except ValueError:
                self.calib_r2ppm_status_var.set(f"Row {i+1}: invalid number")
                return
            if not (0 <= raw <= 32767):
                self.calib_r2ppm_status_var.set(f"Row {i+1}: ADC must be 0\u201332767")
                return
            if ppm < 2.0:
                self.calib_r2ppm_status_var.set(f"Row {i+1}: ppm must be \u2265 2.0")
                return
            v_rl = raw * ADS_LSB_V
            if v_rl <= 0 or v_rl >= VS_V:
                self.calib_r2ppm_status_var.set(f"Row {i+1}: ADC out of valid voltage range")
                return
            rs = R_L_KOHM * (VS_V - v_rl) / v_rl
            points.append((rs, ppm))

        if len(points) == 0:
            self.calib_r2ppm_status_var.set("Enter at least 1 data point")
            return
        if len(points) > 5:
            self.calib_r2ppm_status_var.set("Maximum 5 data points")
            return

        # --- Fit Shah model in log-space ---
        try:
            if len(points) == 1:
                rs, ppm = points[0]
                alpha = SHAH_ALPHA_DEFAULT
                # Invert: R2ppm = Rs * (1 + (ppm-2)/a)^alpha
                r2ppm = rs * ((1.0 + (ppm - 2.0) / SHAH_A) ** alpha)
            else:
                # Linear regression: y_i = b + m*x_i
                # x_i = ln(1 + (ppm_i - 2) / a),  y_i = ln(Rs_i)
                # m = -alpha,  b = ln(R2ppm)
                xs = [math.log(1.0 + (ppm - 2.0) / SHAH_A) for _, ppm in points]
                ys = [math.log(rs) for rs, _ in points]
                n = len(points)
                sum_x  = sum(xs)
                sum_y  = sum(ys)
                sum_xx = sum(x * x for x in xs)
                sum_xy = sum(x * y for x, y in zip(xs, ys))
                denom = n * sum_xx - sum_x * sum_x
                if abs(denom) < 1e-12:
                    raise ValueError("All points have the same ppm \u2014 need variation to fit \u03b1")
                m = (n * sum_xy - sum_x * sum_y) / denom
                b = (sum_y - m * sum_x) / n
                alpha = -m
                r2ppm = math.exp(b)
                if alpha <= 0:
                    raise ValueError(f"Fitted \u03b1 \u2264 0 ({alpha:.4f}) \u2014 check that Rs decreases with ppm")
                if r2ppm <= 0:
                    raise ValueError("Fitted R\u2082ppm \u2264 0 \u2014 check input data")
        except ValueError as exc:
            self.calib_r2ppm_status_var.set(str(exc))
            return

        preview = f"R\u2082ppm={r2ppm:.4f} k\u03a9, \u03b1={alpha:.4f} \u2014 sending\u2026"
        self.calib_r2ppm_status_var.set(preview)
        self.calib_r2ppm_btn.configure(state=tk.DISABLED)
        self._calib_add_btn.configure(state=tk.DISABLED)

        r2ppm_final = r2ppm
        alpha_final = alpha

        def worker() -> None:
            try:
                result = self.client.calib_multipoint(
                    channel=ch, r2ppm_kohm=r2ppm_final, alpha=alpha_final, timeout_s=5.0)
                if result.get("success"):
                    # Automatically timestamp the calibration (unix time, UTC)
                    # and store it in the sensor EEPROM. Non-fatal on older
                    # firmware without the CALIB_TIMESTAMP command.
                    ts_stored = False
                    try:
                        ts_result = self.client.calib_timestamp(
                            channel=ch, epoch=int(time.time()), timeout_s=8.0)
                        ts_stored = bool(ts_result.get("success"))
                    except Exception:
                        ts_stored = False
                    self.events.put(
                        ("calib_r2ppm_ok", (ch_display, r2ppm_final, alpha_final, ts_stored)))
                else:
                    self.events.put(("error", result.get("message", "Calibration failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("calib_r2ppm_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _eeprom_write_ids(self) -> None:
        """Write TGS ID / Lot # / Board ID to the sensor EEPROM after confirming.

        First reads the currently stored values from the device and shows a
        warning dialog with them; only writes if the user confirms.
        """
        # --- Parse & validate inputs ---
        try:
            ch_display = int(self.eeprom_ch_var.get())
            if ch_display < 1:
                raise ValueError
            ch = ch_display - 1  # firmware is 0-based
        except ValueError:
            self.eeprom_status_var.set("Invalid channel (must be \u2265 1)")
            return

        tgs_id_str = self.eeprom_tgs_id_var.get().strip()
        if not tgs_id_str or not tgs_id_str.isdigit() or not (0 <= int(tgs_id_str) <= 65535):
            self.eeprom_status_var.set(
                "TGS ID must be a number 0\u201365535 (calibration index). "
                "Figaro's printed code (e.g. 462VV) goes in Lot #")
            return
        tgs_id = int(tgs_id_str)

        lot = self.eeprom_lot_var.get().strip()
        board = self.eeprom_board_var.get().strip()
        if len(lot) > 15 or len(board) > 31:
            self.eeprom_status_var.set("Lot \u2264 15 chars, Board ID \u2264 31 chars")
            return
        charset_ok = all(
            c.isalnum() and c.isascii() or c in "-_"
            for c in (lot + board)
        )
        if not charset_ok:
            self.eeprom_status_var.set("Lot / Board ID: A-Z a-z 0-9 '_' '-' only")
            return

        self.eeprom_status_var.set("Reading current values\u2026")
        self.eeprom_write_btn.configure(state=tk.DISABLED)

        def read_worker() -> None:
            try:
                result = self.client.eeprom_read_ids(channel=ch, timeout_s=5.0)
                self.events.put(("eeprom_read_done", (result,)))
            except Exception as exc:
                self.events.put(("error", exc))
                self.events.put(("eeprom_read_done", ({"success": False, "message": str(exc)},)))

        threading.Thread(target=read_worker, daemon=True).start()

    def _eeprom_confirm_write(self, current: dict) -> None:
        """Show warning with currently stored values; write on confirmation."""
        try:
            ch_display = int(self.eeprom_ch_var.get())
            ch = ch_display - 1
        except ValueError:
            self._update_wifi_controls()
            return
        tgs_id = int(self.eeprom_tgs_id_var.get().strip())
        lot = self.eeprom_lot_var.get().strip()
        board = self.eeprom_board_var.get().strip()

        if current.get("success"):
            crc = current.get("crc", "?")
            msg = (
                f"Channel {ch_display} \u2014 currently stored values:\n"
                f"  TGS ID:   {current.get('tgs_id', '?')} (CRC {crc})\n"
                f"  Lot #:    {current.get('lot') or '(empty)'}\n"
                f"  Board ID: {current.get('board') or '(empty)'}\n"
                f"  Calibrated: {self._format_calib_time(current.get('calib_time', ''))}\n\n"
                f"This will be overwritten with:\n"
                f"  TGS ID:   {tgs_id}\n"
                f"  Lot #:    {lot or '(empty)'}\n"
                f"  Board ID: {board or '(empty)'}\n\n"
                f"Are you sure you want to continue?"
            )
        else:
            msg = (
                f"Could not read current values "
                f"({current.get('message', 'read failed')}).\n\n"
                f"Write TGS ID={tgs_id}, Lot#={lot or '(empty)'}, "
                f"Board ID={board or '(empty)'} to channel {ch_display} anyway?"
            )

        if not messagebox.askyesno("Confirm EEPROM Write", msg, icon="warning"):
            self.eeprom_status_var.set("Cancelled")
            self._update_wifi_controls()
            return

        self.eeprom_status_var.set("Writing\u2026")

        def write_worker() -> None:
            try:
                result = self.client.eeprom_write_ids(
                    channel=ch, tgs_id=tgs_id, lot=lot, board=board, timeout_s=8.0)
                if result.get("success"):
                    self.events.put(("eeprom_write_ok", (ch_display, tgs_id, lot, board)))
                else:
                    self.events.put(("error", result.get("message", "EEPROM write failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("eeprom_write_done", None))

        threading.Thread(target=write_worker, daemon=True).start()

    @staticmethod
    def _format_calib_time(raw: str) -> str:
        """Format the EEPROM calib-time field (decimal unix time) for display."""
        s = (raw or "").strip()
        if not s:
            return "(never)"
        if s == "ERR":
            return "(invalid)"
        try:
            return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            return s

    def _board_id_write_clicked(self) -> None:
        """Write the mainboard ID to ESP32 NVS after confirmation."""
        bid = self.board_id_var.get().strip()
        if not bid:
            self.board_id_status_var.set("Enter a Board ID first")
            return
        if len(bid) > 31 or not all(
                c.isalnum() and c.isascii() or c in "-_" for c in bid):
            self.board_id_status_var.set("A-Z a-z 0-9 '_' '-' only, max 31 chars")
            return
        if not messagebox.askyesno(
                "Confirm Board ID Write",
                f"Write mainboard ID '{bid}' to the ESP32 memory?\n\n"
                f"It replaces any previously stored Board ID.",
                icon="warning"):
            self.board_id_status_var.set("Cancelled")
            return
        self.board_id_status_var.set("Writing\u2026")
        self.board_id_write_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = self.client.board_id_write(bid, timeout_s=5.0)
                if result.get("success"):
                    self.events.put(("board_id_write_ok", bid))
                else:
                    self.events.put(("error", result.get("message", "Board ID write failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("board_id_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _board_id_read_clicked(self) -> None:
        """Read the mainboard ID from ESP32 NVS and show it in a popup."""
        self.board_id_status_var.set("Reading\u2026")
        self.board_id_read_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = self.client.board_id_read(timeout_s=5.0)
                self.events.put(("board_id_read_ok", result))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("board_id_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _board_id_clear_clicked(self) -> None:
        """Erase the mainboard ID from ESP32 NVS after confirmation."""
        if not messagebox.askyesno(
                "Confirm Board ID Erase",
                "Erase the mainboard ID from the ESP32 memory?\n\n"
                "This cannot be undone.",
                icon="warning"):
            self.board_id_status_var.set("Cancelled")
            return
        self.board_id_status_var.set("Erasing\u2026")
        self.board_id_clear_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = self.client.board_id_clear(timeout_s=5.0)
                if result.get("success"):
                    self.events.put(("board_id_clear_ok", None))
                else:
                    self.events.put(("error", result.get("message", "Board ID erase failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("board_id_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _eeprom_values_text(self, current: dict) -> str:
        """Format stored EEPROM values (from eeprom_read_ids) for display."""
        crc = current.get("crc", "?")
        return (
            f"  TGS ID:   {current.get('tgs_id', '?')}"
            f"{' (BLANK)' if current.get('tgs_id') == '65535' else ''}"
            f"  [CRC {crc}]\n"
            f"  Lot #:    {current.get('lot') or '(empty)'}\n"
            f"  Board ID: {current.get('board') or '(empty)'}\n"
            f"  Calibrated: {self._format_calib_time(current.get('calib_time', ''))}"
        )

    def _eeprom_read_info(self) -> None:
        """Read the stored EEPROM identity values and show them in a popup."""
        try:
            ch_display = int(self.eeprom_ch_var.get())
            if ch_display < 1:
                raise ValueError
            ch = ch_display - 1
        except ValueError:
            self.eeprom_status_var.set("Invalid channel (must be \u2265 1)")
            return

        self.eeprom_status_var.set("Reading\u2026")
        self.eeprom_read_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = self.client.eeprom_read_ids(channel=ch, timeout_s=5.0)
                self.events.put(("eeprom_info_ok", (ch_display, result)))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("eeprom_read_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _eeprom_clear_clicked(self) -> None:
        """Read current values, warn, and erase the EEPROM identity blocks."""
        try:
            ch_display = int(self.eeprom_ch_var.get())
            if ch_display < 1:
                raise ValueError
            ch = ch_display - 1
        except ValueError:
            self.eeprom_status_var.set("Invalid channel (must be \u2265 1)")
            return

        self.eeprom_status_var.set("Reading\u2026")
        self.eeprom_clear_btn.configure(state=tk.DISABLED)

        def worker() -> None:
            try:
                result = self.client.eeprom_read_ids(channel=ch, timeout_s=5.0)
                self.events.put(("eeprom_clear_confirm", (ch_display, result)))
            except Exception as exc:
                self.events.put(("error", exc))
                self.events.put(("eeprom_clear_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _eeprom_do_clear(self, ch_display: int) -> None:
        """Erase identity blocks on ch_display in a background thread."""
        self.eeprom_status_var.set("Erasing\u2026")

        def worker() -> None:
            try:
                result = self.client.eeprom_clear_ids(
                    channel=ch_display - 1, timeout_s=8.0)
                if result.get("success"):
                    self.events.put(("eeprom_clear_ok", ch_display))
                else:
                    self.events.put(("error", result.get("message", "EEPROM clear failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("eeprom_clear_btn_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _fmt_eui(self, raw: str) -> str:
        """Return EUI with or without colon separators based on toggle state."""
        if not raw or ":" not in raw:
            return raw
        return raw if self._lora_colon_var.get() else raw.replace(":", "")

    def _toggle_eui_colons(self) -> None:
        """Re-format EUI fields when the colon toggle changes."""
        self.lora_dev_eui_var.set(self._fmt_eui(self._lora_dev_eui_raw))
        self.lora_app_eui_var.set(self._fmt_eui(self._lora_app_eui_raw))

    def _lora_refresh(self) -> None:
        """Read DevEUI from module and AppEUI/AppKey from device NVS."""
        self.lora_refresh_btn.configure(state=tk.DISABLED)
        self.lora_gen_btn.configure(state=tk.DISABLED)
        self.lora_status_var.set("Reading\u2026")

        def worker() -> None:
            try:
                result = self.client.lora_info(timeout_s=10.0)
                if result.get("success"):
                    self.events.put(("lora_info_ok", result))
                else:
                    self.events.put(("error", result.get("message", "LoRa read failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("lora_done", None))

        threading.Thread(target=worker, daemon=True).start()

    def _lora_gen_keys(self) -> None:
        """Generate new AppEUI + AppKey, program module, store in device NVS."""
        self.lora_refresh_btn.configure(state=tk.DISABLED)
        self.lora_gen_btn.configure(state=tk.DISABLED)
        self.lora_status_var.set("Generating\u2026")

        def worker() -> None:
            try:
                result = self.client.gen_lora_keys(timeout_s=15.0)
                if result.get("success"):
                    result["generated"] = True
                    self.events.put(("lora_info_ok", result))
                else:
                    self.events.put(("error", result.get("message", "Key generation failed")))
            except Exception as exc:
                self.events.put(("error", exc))
            finally:
                self.events.put(("lora_done", None))

        threading.Thread(target=worker, daemon=True).start()

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
        
        # Remember currently selected SSID
        prev_ssid = None
        sel = self.wifi_network_tree.selection()
        if sel:
            vals = self.wifi_network_tree.item(sel[0], "values")
            if vals:
                prev_ssid = vals[0]
        
        for row in self.wifi_network_tree.get_children():
            self.wifi_network_tree.delete(row)

        # Add only scanned networks (not saved networks)
        reselect_iid = None
        for net in networks:
            ssid = net.get("ssid", "")
            rssi = net.get("rssi", "")
            security = net.get("security", "")
            iid = self.wifi_network_tree.insert("", tk.END, values=(ssid, rssi, security))
            if ssid == prev_ssid:
                reselect_iid = iid

        # Add "Other" option at the bottom
        self.wifi_network_tree.insert("", tk.END, values=("Other", "", ""))
        
        # Restore selection if the network is still present
        if reselect_iid:
            self.wifi_network_tree.selection_set(reselect_iid)
            self.wifi_network_tree.see(reselect_iid)

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
                result = self.client.wifi_connect_saved(ssid, timeout_s=60.0)
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
                result = self.client.wifi_forget_by_ssid(ssid, timeout_s=20.0)
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
                result = self.client.wifi_connect(ssid, password, "PSK", timeout_s=60.0)
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
                result = self.client.wifi_connect(ssid, password, "WPA2-EAP", username=username, timeout_s=60.0)
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
                    result = self.client.wifi_connect(ssid, password, auth_type, timeout_s=60.0)
                else:  # WPA2-ENT
                    result = self.client.wifi_connect(ssid, password, auth_type, username=username, timeout_s=60.0)
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

        # Don't send serial commands while live stream is running — they race
        # with _live_loop reads and corrupt framing (same as _rtc_poll_tick).
        if self.live_running:
            self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)
            return

        # Only poll if we're still connected
        if not self.wifi_current_ssid:
            self.wifi_poll_timer = self.after(5000, self._wifi_poll_tick)
            return

        self.wifi_poll_inflight = True

        def worker() -> None:
            try:
                status = self.client.wifi_get_status(timeout_s=8.0)
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

        # During live streaming, RTC time comes from the data timestamps
        # (handled by _update_rtc_from_timestamp). Don't send serial commands
        # that would race with the live loop and corrupt framing.
        if self.live_running:
            self.rtc_poll_timer = self.after(10000, self._rtc_poll_tick)
            return

        self.rtc_poll_inflight = True

        def worker() -> None:
            try:
                result = self.client.get_rtc_time(timeout_s=3.0)
                self.events.put(("rtc_time_ok", result))
            except Exception:
                pass
            finally:
                self.rtc_poll_inflight = False
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

    def mqtt_connect(self) -> None:
        if not HAS_PAHO_MQTT:
            messagebox.showerror("Missing dependency", "Install paho-mqtt first: pip install paho-mqtt")
            return
        broker = self.mqtt_broker_var.get().strip()
        try:
            port = int(self.mqtt_port_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number (e.g. 1883 or 8883)")
            return
        app_id = self.mqtt_app_id_var.get().strip()
        tenant = self.mqtt_tenant_var.get().strip() or "ttn"
        api_key = self.mqtt_api_key_var.get().strip()
        if not app_id or not api_key:
            messagebox.showerror("Missing info", "Application ID and API Key are required")
            return

        username = f"{app_id}@{tenant}"
        topic = f"v3/{username}/devices/+/up"

        client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
        client.username_pw_set(username, api_key)

        def on_connect(_client, _userdata, _flags, rc, _properties=None):
            if rc == 0:
                _client.subscribe(topic)
                self.events.put(("mqtt_status", "connected"))
            else:
                self.events.put(("mqtt_error", f"connect failed rc={rc}"))

        def on_disconnect(_client, _userdata, *_args):
            self.events.put(("mqtt_status", "disconnected"))

        def on_message(_client, _userdata, msg):
            print("MQTT MESSAGE RECEIVED on topic:", msg.topic)
            try:
                payload = json.loads(msg.payload.decode("utf-8"))
            except json.JSONDecodeError:
                return
            device_id = payload.get("end_device_ids", {}).get("device_id", "unknown_device")
            decoded = payload.get("uplink_message", {}).get("decoded_payload")
            if not decoded:
                return
            epoch = decoded.get("utc_epoch")
            timestamp = (
                datetime.fromtimestamp(epoch).isoformat(sep=" ", timespec="seconds")
                if epoch
                else datetime.now().isoformat(sep=" ", timespec="seconds")
            )
            field_map = {
                "scd4x_1_co2":           "co2_ppm",
                "sht45_1_rh_avg":        "rh_pct",
                "tmp117_1_t_avg":        "temp_c",
                "lps22df_1_p_avg":       "pres_hpa",
                "tgs2611_1_ppm_avg":     "ch4_ppm",
                "tgs2611_1_raw_avg":     "tgs2611_raw",
                "sfm3505_1_air_slm_avg": "air_slm",
                "n2o_uart_ppm_avg":      "n2o_ppm",
            }
            row = {"timestamp": timestamp}
            for gui_field, ttn_field in field_map.items():
                row[gui_field] = decoded.get(ttn_field)
            self.events.put(("mqtt_message", (device_id, row)))


        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message

        try:
            client.connect_async(broker, port, keepalive=60)
        except Exception as exc:
            messagebox.showerror("Connection failed", str(exc))
            return

        client.loop_start()
        self.mqtt_client = client
        self.mqtt_status_var.set("Connecting...")
        self.mqtt_connect_btn.configure(state=tk.DISABLED)
        self.mqtt_disconnect_btn.configure(state=tk.NORMAL)

    def mqtt_disconnect(self) -> None:
        if self.mqtt_client is not None:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                pass
            self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_status_var.set("Disconnected")
        self.mqtt_connect_btn.configure(state=tk.NORMAL if HAS_PAHO_MQTT else tk.DISABLED)
        self.mqtt_disconnect_btn.configure(state=tk.DISABLED)

    def _handle_mqtt_status(self, status: str) -> None:
        self.mqtt_connected = status == "connected"
        self.mqtt_status_var.set("Connected" if self.mqtt_connected else "Disconnected")

    def _handle_mqtt_message(self, device_id: str, row: dict) -> None:
        is_new_device = device_id not in self.mqtt_devices
        self.mqtt_devices[device_id] = row

        if is_new_device:
            self.mqtt_history[device_id] = {
                "x": deque(maxlen=self.mqtt_history_limit),
                "ts": deque(maxlen=self.mqtt_history_limit),
                "series": {col: deque(maxlen=self.mqtt_history_limit) for col in self.LORA_COLS},
            }

            values = list(self.mqtt_device_combo.cget("values")) + [device_id]
            self.mqtt_device_combo.configure(values=values)
            if not self.mqtt_selected_device.get():
                self.mqtt_selected_device.set(device_id)

        self.mqtt_row_counter += 1
        hist = self.mqtt_history[device_id]
        hist["x"].append(self.mqtt_row_counter)
        hist["ts"].append(row.get("timestamp", ""))
        for col in self.LORA_COLS:
            val = row.get(col)
            hist["series"][col].append(val if isinstance(val, (int, float)) else None)


        self._append_mqtt_csv_row(device_id, row)

        table_row = [row.get("timestamp", ""), device_id] + [
            "" if row.get(col) is None else row.get(col) for col in self.LORA_COLS
        ]
        self.mqtt_table.insert("", tk.END, values=table_row)
        children = self.mqtt_table.get_children()
        if len(children) > self.mqtt_table_row_limit:
            self.mqtt_table.delete(*children[: len(children) - self.mqtt_table_row_limit])

        if device_id == self.mqtt_selected_device.get():
            self._refresh_mqtt_display()

    def _refresh_mqtt_display(self) -> None:
        device_id = self.mqtt_selected_device.get()
        row = self.mqtt_devices.get(device_id, {})
        self._update_mqtt_cards(row)
        for col in self.LORA_COLS:
            self._redraw_mqtt_graph(device_id, col)

    def _update_mqtt_cards(self, row: dict) -> None:
        for col_name, (val_label, unit_label, _card) in self.mqtt_card_widgets.items():
            val = row.get(col_name)
            if val is None:
                val_label.configure(text="N/A", text_color=self.C_CARD_NA)
            else:
                text = f"{val:.2f}" if isinstance(val, float) else str(val)
                val_label.configure(text=text, text_color=self.C_CARD_NEUTRAL)

    def _redraw_mqtt_graph(self, device_id: str, col: str) -> None:
        canvas = self.mqtt_graph_canvases.get(col)
        if canvas is None:
            return
        hist = self.mqtt_history.get(device_id)
        if not hist:
            canvas.delete("all")
            return
        self._draw_series(canvas, list(hist["x"]), list(hist["ts"]), list(hist["series"][col]))

    def _append_mqtt_csv_row(self, device_id: str, row: dict) -> None:
        self.mqtt_data_dir.mkdir(parents=True, exist_ok=True)
        path = self.mqtt_data_dir / f"{device_id}.csv"
        fieldnames = ["timestamp"] + list(self.LORA_COLS.keys())
        is_new = not path.exists()
        with path.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if is_new:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fieldnames})


    def on_close(self) -> None:
        try:
            self._cancel_wifi_poll()
            self.mqtt_disconnect()
            self.client.close()
        finally:
            self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
