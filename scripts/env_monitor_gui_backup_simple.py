#!/usr/bin/env python3
"""
Desktop GUI for env_monitor serial menu - Binary Protocol Version.

Features:
- Connect/disconnect to serial port
- Start/stop live data stream
- List and download log files from SD
- WiFi management (scan, connect, disconnect)

Uses efficient binary protocol for communication instead of human-readable text.

Requires:
- pyserial
- ttkbootstrap (optional, for better styling)
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

# Try to disable DPI scaling awareness on Windows
try:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# ============ BINARY PROTOCOL DEFINITIONS ============

class ProtoCmd:
    """Binary protocol command codes"""
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
    LOG_MENU = 0x1B
    LOG_LIST = 0x1C
    LOG_GET = 0x1D
    RTC_TIME = 0x1E
    BACK = 0x1F


class ProtoResp:
    """Binary protocol response type codes"""
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


@dataclass
class FileEntry:
    """Log file entry"""
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
        """Open serial port"""
        if self.is_open:
            self.close()
        self.ser = serial.Serial(port, baud, timeout=0.1, write_timeout=2)
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
            try:
                resp = self._read_byte(1.0)
                if resp != ProtoResp.OK:
                    raise RuntimeError(f"Failed to start live: {resp}")
            except:
                pass  # Proceed anyway
        
        self.live_stop.clear()
        self.live_thread = threading.Thread(
            target=self._live_loop,
            args=(on_line,),
            daemon=True,
        )
        self.live_thread.start()

    def _live_loop(self, on_line) -> None:
        """Live stream loop"""
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

    def wifi_connect(self, ssid: str, password: str, auth_type: str = "PSK", username: str = "", timeout_s: float = 20.0) -> dict:
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


# ============ GUI APPLICATION ============

class EnvMonitorGUI:
    """Main GUI application"""

    def __init__(self, root):
        self.root = root
        self.root.title("Env Monitor - Binary Protocol")
        self.root.geometry("900x600")
        
        self.client = SerialMenuClient()
        self.available_ports = []
        
        self.setup_ui()
        self.refresh_ports()
        self.root.after(1000, self.refresh_ports)

    def setup_ui(self):
        """Setup UI components"""
        # Connection frame
        conn_frame = ttk.LabelFrame(self.root, text="Connection", padding=10)
        conn_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(conn_frame, text="Port:").pack(side=tk.LEFT, padx=5)
        self.port_combo = ttk.Combobox(conn_frame, width=20, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(conn_frame, text="Baud:").pack(side=tk.LEFT, padx=5)
        self.baud_combo = ttk.Combobox(conn_frame, values=["9600", "115200", "230400"], width=10)
        self.baud_combo.set("115200")
        self.baud_combo.pack(side=tk.LEFT, padx=5)
        
        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.connect)
        self.connect_btn.pack(side=tk.LEFT, padx=5)
        
        self.disconnect_btn = ttk.Button(conn_frame, text="Disconnect", command=self.disconnect, state=tk.DISABLED)
        self.disconnect_btn.pack(side=tk.LEFT, padx=5)
        
        self.status_label = ttk.Label(conn_frame, text="Not connected")
        self.status_label.pack(side=tk.LEFT, padx=20)
        
        # Notebook for tabs
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        # Live data tab
        live_frame = ttk.Frame(notebook)
        notebook.add(live_frame, text="Live Data")
        self.setup_live_tab(live_frame)
        
        # Logs tab
        logs_frame = ttk.Frame(notebook)
        notebook.add(logs_frame, text="Logs")
        self.setup_logs_tab(logs_frame)
        
        # WiFi tab
        wifi_frame = ttk.Frame(notebook)
        notebook.add(wifi_frame, text="WiFi")
        self.setup_wifi_tab(wifi_frame)

    def setup_live_tab(self, parent):
        """Setup live data tab"""
        btn_frame = ttk.Frame(parent, padding=10)
        btn_frame.pack(fill=tk.X)
        
        self.live_start_btn = ttk.Button(btn_frame, text="Start Stream", command=self.start_live, state=tk.DISABLED)
        self.live_start_btn.pack(side=tk.LEFT, padx=5)
        
        self.live_stop_btn = ttk.Button(btn_frame, text="Stop Stream", command=self.stop_live, state=tk.DISABLED)
        self.live_stop_btn.pack(side=tk.LEFT, padx=5)
        
        # Text display
        scroll = ttk.Scrollbar(parent)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.live_text = tk.Text(parent, yscrollcommand=scroll.set, state=tk.DISABLED)
        self.live_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        scroll.config(command=self.live_text.yview)

    def setup_logs_tab(self, parent):
        """Setup logs tab"""
        btn_frame = ttk.Frame(parent, padding=10)
        btn_frame.pack(fill=tk.X)
        
        self.refresh_logs_btn = ttk.Button(btn_frame, text="Refresh", command=self.refresh_logs, state=tk.DISABLED)
        self.refresh_logs_btn.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(btn_frame, text="Output folder:").pack(side=tk.LEFT, padx=5)
        self.log_folder = ttk.Entry(btn_frame, width=40)
        self.log_folder.pack(side=tk.LEFT, padx=5)
        self.log_folder.insert(0, str(Path.home() / "Downloads"))
        
        ttk.Button(btn_frame, text="Browse", command=self.browse_folder).pack(side=tk.LEFT, padx=5)
        
        # Listbox
        list_frame = ttk.Frame(parent)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        scroll = ttk.Scrollbar(list_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.log_listbox = tk.Listbox(list_frame, yscrollcommand=scroll.set)
        self.log_listbox.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.log_listbox.yview)
        
        self.download_btn = ttk.Button(parent, text="Download Selected", command=self.download_log, state=tk.DISABLED)
        self.download_btn.pack(pady=5)

    def setup_wifi_tab(self, parent):
        """Setup WiFi tab"""
        btn_frame = ttk.Frame(parent, padding=10)
        btn_frame.pack(fill=tk.X)
        
        self.wifi_scan_btn = ttk.Button(btn_frame, text="Scan", command=self.wifi_scan, state=tk.DISABLED)
        self.wifi_scan_btn.pack(side=tk.LEFT, padx=5)
        
        self.wifi_status_btn = ttk.Button(btn_frame, text="Status", command=self.wifi_status, state=tk.DISABLED)
        self.wifi_status_btn.pack(side=tk.LEFT, padx=5)
        
        self.wifi_disconnect_btn = ttk.Button(btn_frame, text="Disconnect", command=self.wifi_disconnect, state=tk.DISABLED)
        self.wifi_disconnect_btn.pack(side=tk.LEFT, padx=5)
        
        # Status display
        self.wifi_status_label = ttk.Label(parent, text="Not connected")
        self.wifi_status_label.pack(padx=10, pady=5)
        
        # Network list
        list_frame = ttk.LabelFrame(parent, text="Available Networks", padding=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        scroll = ttk.Scrollbar(list_frame)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.network_listbox = tk.Listbox(list_frame, yscrollcommand=scroll.set)
        self.network_listbox.pack(fill=tk.BOTH, expand=True)
        scroll.config(command=self.network_listbox.yview)
        
        # Connect frame
        conn_frame = ttk.LabelFrame(parent, text="Connect to Network", padding=10)
        conn_frame.pack(fill=tk.X, padx=10, pady=5)
        
        ttk.Label(conn_frame, text="SSID:").pack(side=tk.LEFT, padx=5)
        self.wifi_ssid = ttk.Entry(conn_frame, width=30)
        self.wifi_ssid.pack(side=tk.LEFT, padx=5)
        
        ttk.Label(conn_frame, text="Password:").pack(side=tk.LEFT, padx=5)
        self.wifi_pwd = ttk.Entry(conn_frame, width=20, show="*")
        self.wifi_pwd.pack(side=tk.LEFT, padx=5)
        
        self.wifi_connect_btn = ttk.Button(conn_frame, text="Connect", command=self.wifi_connect, state=tk.DISABLED)
        self.wifi_connect_btn.pack(side=tk.LEFT, padx=5)

    def refresh_ports(self):
        """Refresh available serial ports"""
        if self.client.is_open:
            return
        
        ports = [p.device for p in list_ports.comports()]
        if ports != self.available_ports:
            self.available_ports = ports
            self.port_combo['values'] = ports
            if ports:
                self.port_combo.set(ports[0])

    def connect(self):
        """Connect to device"""
        port = self.port_combo.get()
        baud = int(self.baud_combo.get())
        
        try:
            self.client.open(port, baud)
            self.status_label.config(text=f"Connected to {port}")
            self.connect_btn.config(state=tk.DISABLED)
            self.disconnect_btn.config(state=tk.NORMAL)
            self.live_start_btn.config(state=tk.NORMAL)
            self.refresh_logs_btn.config(state=tk.NORMAL)
            self.wifi_scan_btn.config(state=tk.NORMAL)
            self.wifi_status_btn.config(state=tk.NORMAL)
            self.wifi_disconnect_btn.config(state=tk.NORMAL)
            self.wifi_connect_btn.config(state=tk.NORMAL)
            messagebox.showinfo("Success", f"Connected to {port}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to connect: {e}")

    def disconnect(self):
        """Disconnect from device"""
        self.client.close()
        self.status_label.config(text="Not connected")
        self.connect_btn.config(state=tk.NORMAL)
        self.disconnect_btn.config(state=tk.DISABLED)
        self.live_start_btn.config(state=tk.DISABLED)
        self.live_stop_btn.config(state=tk.DISABLED)
        self.refresh_logs_btn.config(state=tk.DISABLED)
        self.wifi_scan_btn.config(state=tk.DISABLED)
        self.wifi_status_btn.config(state=tk.DISABLED)
        self.wifi_disconnect_btn.config(state=tk.DISABLED)
        self.wifi_connect_btn.config(state=tk.DISABLED)
        self.download_btn.config(state=tk.DISABLED)

    def start_live(self):
        """Start live streaming"""
        try:
            self.live_text.config(state=tk.NORMAL)
            self.live_text.delete('1.0', tk.END)
            self.live_text.config(state=tk.DISABLED)
            
            self.client.start_live(self.on_live_line)
            self.live_start_btn.config(state=tk.DISABLED)
            self.live_stop_btn.config(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to start live: {e}")

    def on_live_line(self, line: str):
        """Callback for live data lines"""
        self.live_text.config(state=tk.NORMAL)
        self.live_text.insert(tk.END, line + "\n")
        self.live_text.see(tk.END)
        self.live_text.config(state=tk.DISABLED)

    def stop_live(self):
        """Stop live streaming"""
        self.client.stop_live()
        self.live_start_btn.config(state=tk.NORMAL)
        self.live_stop_btn.config(state=tk.DISABLED)

    def refresh_logs(self):
        """Refresh log file list"""
        try:
            self.log_listbox.delete(0, tk.END)
            files = self.client.list_logs()
            for f in files:
                self.log_listbox.insert(tk.END, f"{f.path} ({f.size} bytes)")
            self.download_btn.config(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list logs: {e}")

    def download_log(self):
        """Download selected log"""
        selection = self.log_listbox.curselection()
        if not selection:
            messagebox.showwarning("Warning", "Select a file first")
            return
        
        try:
            folder = Path(self.log_folder.get())
            files = self.client.list_logs()
            file_obj = files[selection[0]]
            
            output = folder / Path(file_obj.path).name
            self.client.download_log(file_obj.index, output)
            messagebox.showinfo("Success", f"Downloaded to {output}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to download: {e}")

    def browse_folder(self):
        """Browse for output folder"""
        folder = filedialog.askdirectory()
        if folder:
            self.log_folder.delete(0, tk.END)
            self.log_folder.insert(0, folder)

    def wifi_scan(self):
        """Scan for WiFi networks"""
        try:
            self.network_listbox.delete(0, tk.END)
            networks = self.client.wifi_scan()
            for net in networks:
                self.network_listbox.insert(tk.END, f"{net['ssid']} ({net['rssi']} dBm, {net['security']})")
        except Exception as e:
            messagebox.showerror("Error", f"Scan failed: {e}")

    def wifi_connect(self):
        """Connect to WiFi"""
        ssid = self.wifi_ssid.get()
        pwd = self.wifi_pwd.get()
        
        if not ssid or not pwd:
            messagebox.showwarning("Warning", "Enter SSID and password")
            return
        
        try:
            result = self.client.wifi_connect(ssid, pwd)
            if result["success"]:
                messagebox.showinfo("Success", result["message"])
            else:
                messagebox.showerror("Error", result["message"])
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def wifi_disconnect(self):
        """Disconnect WiFi"""
        try:
            result = self.client.wifi_disconnect()
            messagebox.showinfo("Info", result["message"])
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def wifi_status(self):
        """Get WiFi status"""
        try:
            status = self.client.wifi_get_status()
            if status["ssid"]:
                msg = f"Connected to: {status['ssid']}\nSignal: {status['rssi']} dBm"
            else:
                msg = "Not connected"
            self.wifi_status_label.config(text=msg)
        except Exception as e:
            messagebox.showerror("Error", str(e))


def main():
    """Main entry point"""
    if HAS_TTKBOOTSTRAP:
        root = tb.Window(themename="darkly")
    else:
        root = tk.Tk()
    
    app = EnvMonitorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
