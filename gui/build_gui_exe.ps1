$ErrorActionPreference = "Stop"

Write-Host "Building env_monitor GUI executable..."

python -m pip install --upgrade pip
python -m pip install pyserial pyinstaller ttkbootstrap

python -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --icon "assets/GEM_icon_256.ico" `
  --collect-data ttkbootstrap `
  --add-data "fonts/DejaVuSansMono-wifi-ramp.ttf:fonts" `
  --add-data "assets/GEM_icon_256.ico:assets" `
  --name "GEM GUI" `
  gui/env_monitor_gui.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\\GEM GUI.exe"
