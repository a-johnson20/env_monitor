#!/usr/bin/env python3
"""
Download a log file from the device's serial menu and save it as CSV.

Requires: pyserial

Examples:
  python gui/download_csv.py --port COM4 --index 1
  python gui/download_csv.py --port COM4 --name 2026-03-04.csv
  python gui/download_csv.py --port COM4 --list
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import serial


LIST_RE = re.compile(r"^\s*(\d+)\)\s+(.+)\s+\((\d+)\s+bytes\)\s*$")
BEGIN_RE = re.compile(r"^BEGIN_LOG\s+(.+)\s+(\d+)\s*$")
END_RE = re.compile(r"^END_LOG\s+(.+)\s+(\d+)\s*$")


@dataclass
class FileEntry:
    index: int
    path: str
    size: int


def send_line(ser: serial.Serial, line: str) -> None:
    ser.write((line + "\n").encode("utf-8"))
    ser.flush()


def read_line(ser: serial.Serial, deadline: float) -> str | None:
    buf = bytearray()
    while time.time() < deadline:
        b = ser.read(1)
        if not b:
            continue
        if b == b"\n":
            return buf.decode("utf-8", errors="replace").strip("\r")
        buf.extend(b)
    return None


def read_menu_lines_until_ready(ser: serial.Serial, timeout_s: float, idle_s: float = 0.35) -> list[str]:
    """
    Read menu text until prompt bytes ("> " / ">") or idle quiet.
    Firmware uses Serial.print("> "), so prompt may not be newline-terminated.
    """
    deadline = time.time() + timeout_s
    last_rx = time.time()
    saw_any = False
    buf = bytearray()
    lines: list[str] = []

    while time.time() < deadline:
        b = ser.read(1)
        if b:
            saw_any = True
            last_rx = time.time()
            buf.extend(b)

            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                raw = bytes(buf[:nl])
                del buf[: nl + 1]
                lines.append(raw.decode("utf-8", errors="replace").rstrip("\r"))

            if buf.endswith(b"> ") or buf.endswith(b">"):
                return lines
        else:
            if saw_any and (time.time() - last_rx) >= idle_s:
                return lines

    return lines


def read_log_export_lines_until_ready(ser: serial.Serial, timeout_s: float, idle_s: float = 0.35) -> list[str]:
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

            if saw_section and (buf.endswith(b"> ") or buf.endswith(b">")):
                return section_lines
        else:
            if saw_section and (time.time() - last_rx) >= idle_s:
                return section_lines

    if saw_section:
        return section_lines
    raise RuntimeError("Timed out waiting for Log Export menu")


def read_until_prompt_collect_files(ser: serial.Serial, timeout_s: float) -> list[FileEntry]:
    lines = read_log_export_lines_until_ready(ser, timeout_s=timeout_s, idle_s=0.35)
    files: list[FileEntry] = []
    for line in lines:
        m = LIST_RE.match(line)
        if m:
            files.append(FileEntry(index=int(m.group(1)), path=m.group(2), size=int(m.group(3))))
    return files


def read_exact(ser: serial.Serial, n: int, timeout_s: float) -> bytes:
    deadline = time.time() + timeout_s
    out = bytearray()
    while len(out) < n and time.time() < deadline:
        chunk = ser.read(n - len(out))
        if not chunk:
            continue
        out.extend(chunk)
    if len(out) != n:
        raise RuntimeError(f"Timed out reading payload: expected {n}, got {len(out)} bytes")
    return bytes(out)


def find_begin_header(ser: serial.Serial, timeout_s: float) -> tuple[str, int]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = read_line(ser, deadline)
        if line is None:
            break
        m = BEGIN_RE.match(line)
        if m:
            return m.group(1), int(m.group(2))
    raise RuntimeError("BEGIN_LOG header not found")


def find_end_footer(ser: serial.Serial, timeout_s: float) -> tuple[str, int]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = read_line(ser, deadline)
        if line is None:
            break
        m = END_RE.match(line)
        if m:
            return m.group(1), int(m.group(2))
    raise RuntimeError("END_LOG footer not found")


def choose_file(files: list[FileEntry], index: int | None, name: str | None) -> FileEntry:
    if not files:
        raise RuntimeError("No files available in /logs")

    if index is not None:
        for f in files:
            if f.index == index:
                return f
        raise RuntimeError(f"Index {index} not found")

    if name:
        for f in files:
            if f.path == name or os.path.basename(f.path) == name:
                return f
        raise RuntimeError(f"File name '{name}' not found")

    return files[0]


def default_output_for(path_on_device: str) -> Path:
    base = os.path.basename(path_on_device) or "download.csv"
    out = Path(base)
    if out.suffix.lower() != ".csv":
        out = out.with_suffix(".csv")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="Serial port, e.g. COM4")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--index", type=int, help="File index from menu list")
    ap.add_argument("--name", help="File path or basename from menu list")
    ap.add_argument("--output", help="Output CSV path (default: selected filename)")
    ap.add_argument("--list", action="store_true", help="List available files and exit")
    ap.add_argument("--timeout", type=float, default=20.0, help="Read timeout seconds")
    args = ap.parse_args()

    with serial.Serial(args.port, args.baud, timeout=0.1, write_timeout=2) as ser:
        # Flush stale startup output and try to return to main menu.
        ser.reset_input_buffer()
        send_line(ser, "b")
        time.sleep(0.15)
        send_line(ser, "b")
        time.sleep(0.15)

        # Enter log export menu.
        send_line(ser, "3")
        files = read_until_prompt_collect_files(ser, args.timeout)

        if args.list:
            if not files:
                print("No files found.", file=sys.stderr)
                return 1
            for f in files:
                print(f"{f.index}: {f.path} ({f.size} bytes)")
            # Leave export menu to resume logger.
            send_line(ser, "b")
            return 0

        chosen = choose_file(files, args.index, args.name)
        print(f"Selected: {chosen.index}) {chosen.path} ({chosen.size} bytes)")
        send_line(ser, str(chosen.index))

        begin_path, begin_size = find_begin_header(ser, args.timeout)
        payload = read_exact(ser, begin_size, max(args.timeout, begin_size / 2048.0))

        # Read trailing newline and footer.
        _ = read_line(ser, time.time() + args.timeout)
        end_path, end_size = find_end_footer(ser, args.timeout)

        if begin_path != end_path or begin_size != end_size:
            raise RuntimeError(
                f"Mismatched transfer metadata: BEGIN=({begin_path},{begin_size}) END=({end_path},{end_size})"
            )
        if len(payload) != end_size:
            raise RuntimeError(f"Payload size mismatch: got {len(payload)} expected {end_size}")

        out_path = Path(args.output) if args.output else default_output_for(begin_path)
        out_path.write_bytes(payload)
        print(f"Saved: {out_path.resolve()} ({len(payload)} bytes)")

        # Back out of export menu so logging can resume.
        send_line(ser, "b")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
