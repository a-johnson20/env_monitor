# Env Monitor GUI

## Run from Python

```powershell
python -m pip install pyserial
python scripts/env_monitor_gui.py
```

## Build portable EXE (Windows)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_gui_exe.ps1
```

Output:

`dist\GEM GUI.exe`

## App flow

1. Connect to your board COM port (typically `115200` baud).
   - File list auto-loads as soon as connection succeeds.
2. `Live Data` tab:
   - Click `Start Live` to enter menu option `1`.
   - Click `Stop Live` to return to menu.
   - Click `Open Graphs` to show live line charts for each variable vs time (auto-starts live stream if needed).
   - Column names are read from the device's `LIVE_HEADER` line when streaming starts.
3. `Files` tab:
   - Click `Refresh File List` to enter menu option `3`.
   - Click a file row to preview CSV contents in table form inside the app.
   - Select a file and click `Download Selected CSV`.
