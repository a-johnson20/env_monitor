$ErrorActionPreference = "Stop"

Write-Host "Building env_monitor GUI executable..."

py -m pip install --upgrade pip
py -m pip install pyserial pyinstaller pillow customtkinter

py -m PyInstaller `
  --noconfirm `
  --onefile `
  --windowed `
  --icon "assets/GEM_icon_256.ico" `
  --collect-data customtkinter `
  --add-data "fonts/DejaVuSansMono-wifi-ramp.ttf:fonts" `
  --add-data "assets/GEM_icon_256.ico:assets" `
  --name "GEM GUI" `
  gui/env_monitor_gui.py

Write-Host ""
Write-Host "Build complete:"
Write-Host "  dist\\GEM GUI.exe"
