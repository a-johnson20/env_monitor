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


class LiveGraphsWindow(tk.Toplevel):
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Live Graphs")
        self.geometry("980x760")
        self.minsize(760, 520)

        self.status_var = tk.StringVar(value="Waiting for live data...")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=10, pady=(8, 4))

        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.scroll_canvas = tk.Canvas(outer, highlightthickness=0)
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
                bg="#ffffff",
                highlightthickness=1,
                highlightbackground="#d0d0d0",
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
        self.geometry("980x680")
        self.minsize(840, 560)

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

        self.port_var = tk.StringVar()
        self.baud_var = tk.StringVar(value="115200")
        self.status_var = tk.StringVar(value="Disconnected")
        self.preview_title_var = tk.StringVar(value="Preview")
        self.preview_info_var = tk.StringVar(value="")

        self._build_ui()
        self._schedule_poll()
        self.refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Port:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=20, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=(6, 10))
        ttk.Button(top, text="Refresh Ports", command=self.refresh_ports).pack(side=tk.LEFT)

        ttk.Label(top, text="Baud:").pack(side=tk.LEFT, padx=(14, 0))
        ttk.Entry(top, textvariable=self.baud_var, width=10).pack(side=tk.LEFT, padx=(6, 10))

        self.connect_btn = ttk.Button(top, text="Connect", command=self.toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=(8, 0))

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        self.live_tab = ttk.Frame(notebook, padding=10)
        self.files_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.live_tab, text="Live Data")
        notebook.add(self.files_tab, text="Files")

        self._build_live_tab()
        self._build_files_tab()

    def _build_live_tab(self) -> None:
        btns = ttk.Frame(self.live_tab)
        btns.pack(fill=tk.X)
        self.live_start_btn = ttk.Button(btns, text="Start Live", command=self.start_live, state=tk.DISABLED)
        self.live_start_btn.pack(side=tk.LEFT)
        self.live_stop_btn = ttk.Button(btns, text="Stop Live", command=self.stop_live, state=tk.DISABLED)
        self.live_stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.live_graphs_btn = ttk.Button(
            btns,
            text="Open Graphs",
            command=self.open_live_graphs,
            state=tk.DISABLED,
        )
        self.live_graphs_btn.pack(side=tk.LEFT, padx=(8, 0))

        split = ttk.Panedwindow(self.live_tab, orient=tk.HORIZONTAL)
        split.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=2)
        split.add(right, weight=1)

        ttk.Label(left, text="Incoming CSV Lines").pack(anchor="w")
        self.live_text = tk.Text(left, wrap="none", height=20)
        self.live_text.pack(fill=tk.BOTH, expand=True)
        self.live_text.configure(state=tk.DISABLED)

        scroll_y = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.live_text.yview)
        self.live_text.configure(yscrollcommand=scroll_y.set)
        scroll_y.place(relx=1.0, rely=0.0, relheight=1.0, anchor="ne")

        ttk.Label(right, text="Latest Row").pack(anchor="w")
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
            btns, text="Refresh File List", command=self.refresh_files, state=tk.DISABLED
        )
        self.refresh_files_btn.pack(side=tk.LEFT)
        self.download_btn = ttk.Button(
            btns, text="Download Selected CSV", command=self.download_selected, state=tk.DISABLED
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

        ttk.Label(bottom, textvariable=self.preview_title_var).pack(anchor="w")
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

        ttk.Label(bottom, textvariable=self.preview_info_var).pack(anchor="w", pady=(4, 0))

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
                    self.status_var.set(f"Connected: {payload}")
                    self.connect_btn.configure(text="Disconnect")
                    self.live_start_btn.configure(state=tk.NORMAL)
                    self.live_graphs_btn.configure(state=tk.NORMAL)
                    self._update_files_controls()
                    # Auto-load SD file list right after serial connection opens.
                    self.refresh_files()
                elif kind == "disconnected":
                    self.connected = False
                    self.live_running = False
                    self.files_refresh_inflight = False
                    self.preview_loading_index = None
                    self.status_var.set("Disconnected")
                    self.connect_btn.configure(text="Connect")
                    self.live_start_btn.configure(state=tk.DISABLED)
                    self.live_stop_btn.configure(state=tk.DISABLED)
                    self.live_graphs_btn.configure(state=tk.DISABLED)
                    self._update_files_controls()
                    self.file_entries = []
                    self.preview_cache.clear()
                    for row in self.files_tree.get_children():
                        self.files_tree.delete(row)
                    self._set_preview_message("", "Connect and refresh files to preview.", 0)
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
                    self._update_files_controls()
                elif kind == "live_stopped":
                    self.live_running = False
                    self.status_var.set("Connected")
                    self.live_start_btn.configure(state=tk.NORMAL)
                    self.live_stop_btn.configure(state=tk.DISABLED)
                    self._update_files_controls()
                elif kind == "refresh_done":
                    self.files_refresh_inflight = False
                    self._update_files_controls()
                elif kind == "busy":
                    self.status_var.set(str(payload))
        except Empty:
            pass
        self._schedule_poll()

    def refresh_ports(self) -> None:
        ports = [p.device for p in list_ports.comports()]
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def toggle_connection(self) -> None:
        if self.client.is_open:
            self.client.close()
            self.events.put(("disconnected", None))
            return

        port = self.port_var.get().strip()
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
        for f in files:
            self.files_tree.insert("", tk.END, values=(f.index, f.path, f.size))
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

        for row in rows:
            self.preview_tree.insert("", tk.END, values=tuple(row))

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
        keys = self.live_headers
        if len(keys) != len(fields):
            keys = ["timestamp"] + [f"col_{i}" for i in range(1, len(fields))]

        for row in self.latest_tree.get_children():
            self.latest_tree.delete(row)
        for i, value in enumerate(fields):
            key = keys[i] if i < len(keys) else f"col_{i}"
            self.latest_tree.insert("", tk.END, values=(key, value))

        self._update_live_history(fields)
        self._refresh_live_graphs()

    def on_close(self) -> None:
        try:
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
