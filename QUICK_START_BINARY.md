# Quick Reference: Using the New Binary Protocol GUI

## Installation & Setup

### Prerequisites
```bash
pip install pyserial ttkbootstrap  # Optional: ttkbootstrap for better styling
```

### Running the GUI
```bash
cd scripts
python env_monitor_gui.py
```

## GUI Operations

### 1. Connection
- Select COM port from dropdown
- Confirm baud rate (default: 115200)
- Click "Connect"
- Status shows when connected

### 2. Live Data
- Click "Start Stream" to begin receiving sensor data
- Data displays in real-time (CSV format)
- Click "Stop Stream" to pause

### 3. Logs
- Click "Refresh" to list available log files
- Select a log file
- Specify output folder
- Click "Download Selected"
- File saves to chosen location

### 4. WiFi
- **Scan**: Click "Scan" to find available networks
- **Connect**: 
  - Enter SSID and password
  - Click "Connect"
- **Status**: Click "Status" to see current connection
- **Disconnect**: Click "Disconnect" to drop connection

## What's Different from Old Version

### Binary Protocol Benefits
✓ **Faster**: ~90% less data transmitted
✓ **Simpler**: No text parsing
✓ **More Reliable**: Binary encoding is unambiguous
✓ **Same Features**: All functionality preserved

### What's the Same
- Same user interface
- Same features and operations
- Same live data CSV format
- Same log file format
- Same WiFi management

## Troubleshooting

### "Serial port not open" Error
- Check USB cable connection
- Verify COM port selection
- Ensure device is powered on
- Try different USB port

### Connection Timeout
- Increase timeout in code (if needed)
- Check baud rate matches device (usually 115200)
- Verify device is ready (wait 2-3 seconds after power-on)

### WiFi Connection Fails
- Verify SSID and password
- Check device WiFi module status
- Check device is in range of network
- Try scanning first to verify networks are visible

### GUI Won't Start
- Ensure Python 3.7+ is installed
- Install required packages: `pip install pyserial`
- Check for errors in console output
- Try running from project root: `python scripts/env_monitor_gui.py`

## Protocol Details (For Developers)

See `BINARY_PROTOCOL.md` for:
- Complete command reference
- Response format specifications
- Error codes
- Manual protocol testing
- Implementation examples

## Performance Metrics

### Example: WiFi Scan + Connect
**Old (Text)**: 
- 200+ bytes of menu output
- 500+ bytes of network list
- 100+ bytes of prompts/responses
- **Total: ~850 bytes**

**New (Binary)**:
- 1 byte command
- 40-60 bytes network list
- 1-2 bytes for connect command
- 1 byte for response
- **Total: ~50 bytes**

**Result: 17x less data! (~94% reduction)**

## Key Files

```
scripts/env_monitor_gui.py          - Main GUI application
include/ui/serial_protocol.hpp      - Protocol definitions
src/ui/serial_protocol.cpp          - Protocol helpers
src/ui/serial_menu.cpp              - Device-side protocol handler
BINARY_PROTOCOL.md                  - Technical specification
SERIAL_OPTIMIZATION.md              - Project completion summary
```

## Backing Up Original

The original text-based GUI is preserved as:
```
scripts/env_monitor_gui.py.bak
```

If you need to revert to the old protocol:
1. Keep old firmware compiled (don't update)
2. Use `env_monitor_gui.py.bak` instead of `env_monitor_gui.py`

**Do not mix**: Old firmware with new GUI or vice versa!

## Commands Quick Reference

### WiFi Commands
| Operation | Binary Cmd |
|-----------|----------|
| Scan Networks | 0x14 |
| Connect PSK | 0x15 |
| Connect EAP | 0x16 |
| List Saved | 0x17 |
| Disconnect | 0x18 |
| Get Status | 0x1A |

### Log Commands
| Operation | Binary Cmd |
|-----------|----------|
| List Files | 0x1C |
| Download File | 0x1D |

### Data Commands
| Operation | Binary Cmd |
|-----------|----------|
| Start Live | 0x11 |
| Stop Live | 0x12 |
| Get RTC Time | 0x1E |

## Support

For issues or questions:
1. Check `BINARY_PROTOCOL.md` for technical details
2. Review `SERIAL_OPTIMIZATION.md` for design rationale
3. Check console output for error messages
4. Verify firmware and GUI versions match (both binary or both text)

---

**Happy monitoring!** 🎉

The new binary protocol provides significantly faster communication with the same great features.
