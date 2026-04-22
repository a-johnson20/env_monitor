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
    LIVE_DATA = 0x20
    RTC_RESPONSE = 0x21
    PUMP_STATUS = 0x22
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

    # ============ COMMAND METHODS ============

    def list_logs(self, timeout_s: float = 20.0) -> list[FileEntry]:
        """List log files"""
        if self.live_thread is not None and self.live_thread.is_alive():
            raise RuntimeError("Stop live stream before listing files")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            self._send_cmd(ProtoCmd.LOG_LIST)
            
            # Read response
            resp_type = self._read_byte(timeout_s)
            
            if resp_type == ProtoResp.ERROR:
                error_code = self._read_byte(timeout_s)
                raise RuntimeError(f"Device error {error_code}")
            
            if resp_type != ProtoResp.LOG_LIST:
                raise RuntimeError(f"Unexpected response: {resp_type}")
            
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
            
            # Read response
            resp_type = self._read_byte(timeout_s)
            
            if resp_type == ProtoResp.ERROR:
                error_code = self._read_byte(timeout_s)
                raise RuntimeError(f"Device error {error_code}")
            
            if resp_type != ProtoResp.LOG_BEGIN:
                raise RuntimeError(f"Expected LOG_BEGIN, got {resp_type}")
            
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

    def start_live(self, on_line) -> None:
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
            args=(on_line,),
            daemon=True,
        )
        self.live_thread.start()

    def _live_loop(self, on_line) -> None:
        """Live stream loop - reads typed messages from device
        
        Format: [type byte][length byte][message data]
        Types: 0x02 = STATUS (debug), 0x20 = LIVE_DATA (CSV)
        Only CSV data (0x20) is passed to on_line callback.
        """
        ser = self._require_open()
        
        while not self.live_stop.is_set():
            try:
                # Read message type
                b = ser.read(1)
                if not b:
                    continue
                
                msg_type = b[0]
                
                # Read message length
                b = ser.read(1)
                if not b:
                    continue
                
                length = b[0]
                if length == 0:
                    continue  # Skip zero-length messages
                
                # Read message data
                msg_data = self._read_exact(length, timeout_s=5.0)
                line = msg_data.decode("utf-8", errors="replace")
                
                # Only process LIVE_DATA (0x20) messages
                if msg_type == 0x20:
                    if line:
                        on_line(line)
                # Ignore STATUS (0x02) debug messages
            except Exception:
                # On any serial error, drain the buffer to resync framing
                try:
                    ser.reset_input_buffer()
                except Exception:
                    pass
        
        # Stop live mode
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
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            self._send_cmd(ProtoCmd.WIFI_SCAN)
            
            # Read response
            resp_type = self._read_byte(timeout_s)
            
            if resp_type == ProtoResp.ERROR:
                return []
            
            if resp_type != ProtoResp.WIFI_LIST:
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
                
                # Read response
                resp_type = self._read_byte(timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Connected"}
                elif resp_type == ProtoResp.ERROR:
                    self._read_byte(timeout_s)
                    return {"success": False, "ssid": ssid, "message": "Connection failed"}
                else:
                    return {"success": False, "ssid": ssid, "message": f"Unexpected response: {resp_type}"}
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
                
                resp_type = self._read_byte(timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "message": "Disconnected"}
                else:
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
            
            try:
                self._send_cmd(ProtoCmd.WIFI_STATUS)
                
                resp_type = self._read_byte(timeout_s)
                
                if resp_type != ProtoResp.WIFI_INFO:
                    return {"ssid": "", "rssi": ""}
                
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
            except Exception:
                return {"ssid": "", "rssi": ""}

    def wifi_get_saved_networks(self, timeout_s: float = 5.0) -> list[str]:
        """Get saved WiFi networks"""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            
            try:
                self._send_cmd(ProtoCmd.WIFI_SAVED_LIST)
                
                resp_type = self._read_byte(timeout_s)
                
                if resp_type != ProtoResp.WIFI_SAVED:
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
                
                resp_type = self._read_byte(timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Connected"}
                elif resp_type == ProtoResp.ERROR:
                    self._read_byte(timeout_s)
                    return {"success": False, "ssid": ssid, "message": "Connection failed"}
                else:
                    return {"success": False, "ssid": ssid, "message": f"Unexpected response: {resp_type}"}
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
                
                # Read response
                resp_type = self._read_byte(timeout_s)
                
                if resp_type == ProtoResp.OK:
                    return {"success": True, "ssid": ssid, "message": "Network forgotten"}
                elif resp_type == ProtoResp.ERROR:
                    error_code = self._read_byte(timeout_s)
                    return {"success": False, "ssid": ssid, "message": f"Error code {error_code}"}
                else:
                    return {"success": False, "ssid": ssid, "message": f"Unexpected response: {resp_type}"}
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
                
                resp_type = self._read_byte(timeout_s)
                
                if resp_type != ProtoResp.RTC_RESPONSE:
                    return {"success": False, "time": ""}
                
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
        """Set pump speed (0-100%)."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        pct = max(0, min(100, percent))
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.PUMP_SET)
            ser.write(bytes([pct]))
            ser.flush()
            resp = self._read_byte(timeout_s)
            if resp == ProtoResp.OK:
                return {"success": True, "percent": pct}
            elif resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            return {"success": False, "message": f"Unexpected response: {resp}"}

    def pump_get(self, timeout_s: float = 3.0) -> dict:
        """Get current pump speed."""
        if not self.is_open:
            raise RuntimeError("Serial port not open")
        with self.lock:
            ser = self._require_open()
            ser.reset_input_buffer()
            self._send_cmd(ProtoCmd.PUMP_GET)
            resp = self._read_byte(timeout_s)
            if resp == ProtoResp.PUMP_STATUS:
                length = self._read_byte(timeout_s)
                pct = self._read_byte(timeout_s)
                return {"success": True, "percent": pct}
            elif resp == ProtoResp.ERROR:
                code = self._read_byte(timeout_s)
                return {"success": False, "message": f"Error code {code}"}
            return {"success": False, "message": f"Unexpected response: {resp}"}


# ============ GUI APPLICATION ============






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
        self.rtc_current_datetime = "0000-00-00 00:00"

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
        self.rtc_time_label = tk.Label(right_frame, text=self.rtc_current_datetime, font=standard_font, bg=self.c_bg, fg=self.c_muted)
        self.rtc_time_label.pack(side=tk.LEFT, padx=(12, 0))
        
        # Name label on left uses standard font
        self.wifi_name_label = tk.Label(wifi_frame, text="", font=standard_font, bg=self.c_bg, fg=self.c_muted)
        self.wifi_name_label.pack(side=tk.LEFT)
        
        # Icon label on right uses WiFi font
        self.wifi_icon_label = tk.Label(wifi_frame, text="", font=wifi_icon_font, bg=self.c_bg, fg=self.c_muted)
        self.wifi_icon_label.pack(side=tk.LEFT, padx=(4, 0))
        
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

        # Create split pane: table on top, graphs on bottom
        split = ttk.Panedwindow(self.live_tab, orient=tk.VERTICAL)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        # Top: Table
        table_frame = ttk.Frame(split)
        split.add(table_frame, weight=2)
        
        ttk.Label(table_frame, text="Live Data Table", style="Section.TLabel").pack(anchor="w")
        
        # Create frame to hold table and scrollbars using grid layout
        table_inner = ttk.Frame(table_frame)
        table_inner.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        table_inner.rowconfigure(0, weight=1)
        table_inner.columnconfigure(0, weight=1)
        
        # Create Treeview for live data table
        self.live_table = ttk.Treeview(table_inner, show="headings")
        self.live_table.grid(row=0, column=0, sticky="nsew")
        
        # Add scrollbars
        scroll_y = ttk.Scrollbar(table_inner, orient=tk.VERTICAL, command=self.live_table.yview)
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x = ttk.Scrollbar(table_inner, orient=tk.HORIZONTAL, command=self.live_table.xview)
        scroll_x.grid(row=1, column=0, sticky="ew")
        self.live_table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        
        # Will populate columns when header arrives
        self.live_table_columns = []

        # Bottom: Graphs
        graphs_frame = ttk.Frame(split)
        split.add(graphs_frame, weight=1)
        
        ttk.Label(graphs_frame, text="Live Graphs", style="Section.TLabel").pack(anchor="w")
        
        # Create scrollable canvas for graphs
        self.graphs_canvas = tk.Canvas(graphs_frame, highlightthickness=0, bg=self.c_bg)
        self.graphs_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        graphs_vsb = ttk.Scrollbar(graphs_frame, orient=tk.VERTICAL, command=self.graphs_canvas.yview)
        graphs_vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.graphs_canvas.configure(yscrollcommand=graphs_vsb.set)

        self.graphs_content = ttk.Frame(self.graphs_canvas)
        self.graphs_content_id = self.graphs_canvas.create_window((0, 0), window=self.graphs_content, anchor="nw")
        self.graphs_content.bind("<Configure>", self._on_graphs_content_configure)
        self.graphs_canvas.bind("<Configure>", self._on_graphs_canvas_configure)
        
        self.graph_names: list[str] = []
        self.graph_canvases: list[tk.Canvas] = []
        self._graph_redraw_pending = False

    def _on_graphs_content_configure(self, _event=None) -> None:
        self.graphs_canvas.configure(scrollregion=self.graphs_canvas.bbox("all"))

    def _on_graphs_canvas_configure(self, event) -> None:
        self.graphs_canvas.itemconfigure(self.graphs_content_id, width=event.width)

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

        # ---- Pump Control Section ----
        pump_frame = ttk.LabelFrame(self.wifi_tab, text="Pump Control")
        pump_frame.pack(fill=tk.X, pady=(12, 0))

        slider_row = ttk.Frame(pump_frame)
        slider_row.pack(fill=tk.X, padx=10, pady=(8, 4))

        ttk.Label(slider_row, text="Speed:").pack(side=tk.LEFT)
        self.pump_pct_var = tk.IntVar(value=0)
        self.pump_slider = ttk.Scale(
            slider_row, from_=0, to=100, orient=tk.HORIZONTAL,
            variable=self.pump_pct_var,
            command=lambda _: self.pump_label_var.set(f"{int(self.pump_pct_var.get())}%"),
        )
        self.pump_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 8))
        self.pump_label_var = tk.StringVar(value="0%")
        ttk.Label(slider_row, textvariable=self.pump_label_var, width=5).pack(side=tk.LEFT)

        btn_row = ttk.Frame(pump_frame)
        btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.pump_set_btn = ttk.Button(
            btn_row, text="Set", command=self._pump_set, state=tk.DISABLED, style="Accent.TButton",
        )
        self.pump_set_btn.pack(side=tk.LEFT)

        self.pump_off_btn = ttk.Button(
            btn_row, text="Off", command=self._pump_off, state=tk.DISABLED,
        )
        self.pump_off_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.pump_full_btn = ttk.Button(
            btn_row, text="100%", command=self._pump_full, state=tk.DISABLED,
        )
        self.pump_full_btn.pack(side=tk.LEFT, padx=(8, 0))

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
                    # Grey out file/WiFi buttons during initialization
                    self.refresh_files_btn.configure(state=tk.DISABLED)
                    self.download_btn.configure(state=tk.DISABLED)
                    self.wifi_scan_btn.configure(state=tk.DISABLED)
                    self.wifi_saved_btn.configure(state=tk.DISABLED)
                    self.wifi_reset_btn.configure(state=tk.DISABLED)
                    self.refresh_ports()
                    # Start RTC polling
                    self._schedule_rtc_poll()
                    # Fetch initial WiFi status
                    self._initial_status_fetch()
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
                    # Clear live table and graphs
                    self.live_table.delete(*self.live_table.get_children())
                    self.live_table_columns = []
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
                    # Clear table and graphs
                    self.live_table.delete(*self.live_table.get_children())
                    self.live_table_columns = []
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
                        self._update_wifi_top_bar()
                        self._schedule_wifi_poll()
                        self.status_var.set(f"Connected to {result['ssid']}")
                        # Fetch status, saved networks, and rescan in a single thread
                        # to avoid overlapping serial commands
                        def post_connect() -> None:
                            try:
                                status = self.client.wifi_get_status(timeout_s=5.0)
                                self.events.put(("wifi_status_ok", status))
                            except Exception:
                                pass
                            try:
                                networks = self.client.wifi_get_saved_networks(timeout_s=10.0)
                                self.events.put(("wifi_saved_ok", networks))
                            except Exception:
                                pass
                            try:
                                nets = self.client.wifi_scan(timeout_s=15.0)
                                self.events.put(("wifi_scan_ok", nets))
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
                    
                    # Update if values changed
                    ssid_changed = (self.wifi_current_ssid != new_ssid)
                    rssi_changed = (self.wifi_current_rssi != new_rssi)
                    
                    if ssid_changed or rssi_changed:
                        self.wifi_current_ssid = new_ssid
                        self.wifi_current_rssi = new_rssi
                        self._update_wifi_top_bar()
                    
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
                    self.status_var.set(f"Pump set to {payload}%")
                elif kind == "pump_done":
                    self._update_wifi_controls()
                elif kind == "pump_init":
                    pct = int(payload)
                    self.pump_pct_var.set(pct)
                    self.pump_label_var.set(f"{pct}%")
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

    def _initial_status_fetch(self) -> None:
        """Fetch WiFi status and pump speed after COM connect."""
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
                    self.events.put(("pump_init", pump["percent"]))
            except Exception:
                pass
            finally:
                self.events.put(("initial_status_done", None))

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

    def _refresh_live_graphs(self) -> None:
        if len(self.live_headers) <= 1 and self.live_series:
            self.live_headers = ["timestamp"] + [f"col_{i}" for i in range(1, len(self.live_series) + 1)]
        
        var_names = self.live_headers[1:] if len(self.live_headers) > 1 else []
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

        for name in var_names:
            pane = ttk.LabelFrame(self.graphs_content, text=name)
            pane.pack(fill=tk.X, padx=8, pady=6)
            canvas = tk.Canvas(
                pane,
                height=170,
                bg=self.c_surface,
                highlightthickness=1,
                highlightbackground=self.c_border,
            )
            canvas.pack(fill=tk.X, expand=True, padx=6, pady=6)
            self.graph_canvases.append(canvas)

    def _redraw_graphs(self) -> None:
        """Redraw all graphs with current data."""
        if not self.graph_canvases or not self.live_x_values:
            return
            
        for i, canvas in enumerate(self.graph_canvases):
            y_vals = list(self.live_series[i]) if i < len(self.live_series) else []
            self._draw_series(canvas, list(self.live_x_values), list(self.live_time_labels), y_vals)

    @staticmethod
    def _draw_series(
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
        # Check for header line: first field is "timestamp"
        if line.startswith("timestamp"):
            # Parse header CSV
            hdr = [field.strip() for field in line.split(",")]
            hdr = [field for field in hdr if field]
            if hdr:
                self.live_headers = hdr
                # Set up table columns
                self.live_table_columns = hdr
                self.live_table.configure(columns=hdr)
                for col in hdr:
                    self.live_table.heading(col, text=col)
                    # Make timestamp column wider, others narrower
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
        # Pad with empty strings if needed
        while len(fields) < len(self.live_table_columns):
            fields.append("")
        
        # Insert row
        row_id = self.live_table.insert("", tk.END, values=fields)
        
        # Keep table size reasonable (max 2000 rows)
        all_items = self.live_table.get_children()
        if len(all_items) > 2000:
            # Delete oldest 500 rows
            for item in all_items[:500]:
                self.live_table.delete(item)
        
        # Auto-scroll to bottom
        self.live_table.see(row_id)

        # Try to extract and update RTC time from first field (timestamp)
        if fields and len(fields) > 0:
            self._update_rtc_from_timestamp(fields[0])

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
        """Enable/disable WiFi and pump buttons based on connection state and live streaming."""
        # Disable WiFi controls during live streaming to avoid interference
        wifi_ok = self.connected and not self.live_running
        self.wifi_scan_btn.configure(state=tk.NORMAL if wifi_ok and not self.wifi_scan_inflight else tk.DISABLED)
        self.wifi_saved_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)
        self.wifi_reset_btn.configure(state=tk.NORMAL if wifi_ok else tk.DISABLED)
        # Pump controls
        pump_ok = self.connected and not self.live_running
        pump_st = tk.NORMAL if pump_ok else tk.DISABLED
        self.pump_set_btn.configure(state=pump_st)
        self.pump_off_btn.configure(state=pump_st)
        self.pump_full_btn.configure(state=pump_st)

    def _pump_set(self) -> None:
        """Set pump to slider value."""
        pct = int(self.pump_pct_var.get())
        self._pump_send(pct)

    def _pump_off(self) -> None:
        """Turn pump off."""
        self.pump_pct_var.set(0)
        self.pump_label_var.set("0%")
        self._pump_send(0)

    def _pump_full(self) -> None:
        """Set pump to 100%."""
        self.pump_pct_var.set(100)
        self.pump_label_var.set("100%")
        self._pump_send(100)

    def _pump_send(self, pct: int) -> None:
        """Send pump speed command in background thread."""
        self.pump_set_btn.configure(state=tk.DISABLED)
        self.pump_off_btn.configure(state=tk.DISABLED)
        self.pump_full_btn.configure(state=tk.DISABLED)

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

    def on_close(self) -> None:
        try:
            self._cancel_wifi_poll()
            self.client.close()
        finally:
            self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

