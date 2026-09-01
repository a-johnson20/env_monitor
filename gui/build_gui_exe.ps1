$ErrorActionPreference = "Stop"

Write-Host "Building env_monitor GUI executable..."

py -m pip install --upgrade pip
# ttkbootstrap 2.x is a breaking rewrite (renamed themes e.g. united -> united-light,
# and checkbox/slider rendering regressions) - pin to the last 1.x release.
py -m pip install pyserial pyinstaller "ttkbootstrap<2"

py -m PyInstaller `
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
