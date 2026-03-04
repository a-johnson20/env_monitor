$ErrorActionPreference = "Stop"

Write-Host "Building env_monitor GUI executable..."

python -m pip install --upgrade pip
python -m pip install pyserial pyinstaller

python -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --name "GEM GUI" `
  scripts/env_monitor_gui.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\\GEM GUI.exe"
