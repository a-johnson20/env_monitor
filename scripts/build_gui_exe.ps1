$ErrorActionPreference = "Stop"

Write-Host "Building env_monitor GUI executable..."

python -m pip install --upgrade pip
python -m pip install pyserial pyinstaller ttkbootstrap

python -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --collect-data ttkbootstrap `
  --add-data "fonts/DejaVuSansMono-wifi-ramp.ttf:fonts" `
  --name "GEM GUI" `
  scripts/env_monitor_gui.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\\GEM GUI.exe"
