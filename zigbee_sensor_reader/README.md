# Sonoff Zigbee Sensor Reader

Python application to collect temperature and humidity data from **Sonoff SNZB-02** (and similar) Zigbee sensors via a **Sonoff Zigbee Dongle-M** connected over Ethernet.

## Features

- 🌡️ Reads temperature, humidity, and battery level from 10+ sensors
- 🌐 Connects to Dongle-M over your local network (Ethernet/TCP socket)
- 💾 Stores readings in a local SQLite database
- 📊 Exports to **CSV** and **Excel** for Power BI / Excel analysis
- 🔗 Auto-discovers new sensors when they join the network
- 🏷️ Configurable friendly names per sensor (e.g. "Kitchen", "Bedroom")

## Hardware Required

| Item | Notes |
|------|-------|
| **Sonoff Zigbee Dongle-M** | Coordinator connected via Ethernet (EFR32MG21, EZSP protocol) |
| **Sonoff SNZB-02 / SNZB-02D / SNZB-02P** | Temperature & humidity sensors |

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Verify network connectivity

Make sure the Dongle-M is reachable on your network:

```bash
ping 192.168.1.59
```

The dongle exposes a TCP serial socket (typically port `8888`). You can test connectivity:

```bash
python -c "import socket; s=socket.socket(); s.settimeout(3); s.connect(('192.168.1.59', 8888)); print('Connected!'); s.close()"
```

### 3. Configure

Edit `zigbee_sensor_reader/config.py` or set environment variables:

```bash
# Set the dongle IP (default: 192.168.1.59)
set ZIGBEE_HOST=192.168.1.59

# Set the TCP port (default: 8888)
set ZIGBEE_PORT=8888

# Or override the full device path directly
set ZIGBEE_DEVICE_PATH=socket://192.168.1.59:8888

# Set polling interval in seconds (default: 60)
set ZIGBEE_POLL_INTERVAL=30
```

### 4. Pair sensors

Run in pairing mode, then hold the sensor button for 5+ seconds:

```bash
python -m zigbee_sensor_reader --pair
```

The program will print each sensor's IEEE address as it joins. Add friendly names in `config.py`:

```python
SENSOR_NAMES = {
    "00:12:4b:00:25:e7:a1:c3": "Living Room",
    "00:12:4b:00:25:e7:a1:c4": "Kitchen",
}
```

## Usage

### Collect data (runs continuously)

```bash
python -m zigbee_sensor_reader
```

### View sensor summary

```bash
python -m zigbee_sensor_reader --summary
```

### Export to CSV

```bash
python -m zigbee_sensor_reader --export csv
python -m zigbee_sensor_reader --export csv --start 2026-01-01 --end 2026-06-30
```

### Export to Excel (with per-sensor sheets)

```bash
python -m zigbee_sensor_reader --export xlsx
```

## Power BI Integration

1. Export to CSV or Excel using the commands above
2. In Power BI Desktop: **Get Data → Text/CSV** or **Excel**
3. The "All Sensors" sheet contains all data; per-sensor sheets are also available
4. Key columns for analysis: `Timestamp`, `Sensor Name`, `Temperature (°C)`, `Humidity (%)`

Alternatively, connect Power BI directly to the SQLite database (`sensor_data.db`) using an ODBC driver.

## Project Structure

```
zigbee_sensor_reader/
├── __init__.py
├── __main__.py      # CLI entry point
├── config.py        # Configuration (serial port, sensor names, etc.)
├── database.py      # SQLite storage layer
├── zigbee_reader.py # Zigbee coordinator + sensor communication
└── export.py        # CSV and Excel export functions
```
