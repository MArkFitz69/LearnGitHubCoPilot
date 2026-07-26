# Sonoff Zigbee Sensor Reader

Python application to monitor home temperature and humidity for heating analysis. Collects data from multiple sources and stores it for Power BI visualization.

## Data Sources

| Source | Protocol | Data Collected |
|--------|----------|----------------|
| **Sonoff SNZB-02D/DR2** sensors | Zigbee via Dongle-M (direct EZSP) **or** Zigbee2MQTT (MQTT) | Temperature, humidity, battery |
| **Hive thermostats** | Cloud API | Temperature, target, heating on/off, boost, mode |
| **Shelly Blu H&T** | Bluetooth (BLE) | Temperature, humidity, battery |

## Features

- 🌡️ Reads temperature, humidity, and battery from 10+ Zigbee sensors
- 🔥 Captures Hive thermostat state: current temp, target, heating on/off, boost, mode
- 📡 Captures Shelly Blu H&T outdoor sensor via Bluetooth
- 🏠 Heating zone mapping (Zone 1/2/3) for each sensor and thermostat
- 🌐 Web API server for remote data access from Power BI
- ➕ Secure web onboarding flow for adding one Zigbee sensor at a time
- ❤️ Zigbee heartbeat telemetry and offline-risk detection on `/system`
- 💾 SQLite database with automatic schema migrations
- 📊 CSV and Excel export with per-sensor sheets

## Hardware

| Item | Role |
|------|------|
| **Raspberry Pi 3 B+** | 24/7 data collector (Bluetooth + network) |
| **Sonoff Zigbee Dongle-M** | Zigbee coordinator (Ethernet at 192.168.1.59:6638) |
| **Sonoff SNZB-02D / SNZB-02DR2** × 8 | Indoor temp/humidity sensors |
| **Hive Thermostats** × 3 | Heating system control (cloud API) |
| **Shelly Blu H&T** × 1 | Outdoor temp/humidity (BLE) |

## Heating Zones

| Zone | Thermostat | Sensors |
|------|-----------|---------|
| **Zone 1** (Ground) | Hall | Living Room, Dining Room, Porch |
| **Zone 2** (First floor) | Master Bedroom | Guest Bedroom, Ensuite |
| **Zone 3** (Top floor) | Top Floor Landing | Blanca Room, Stellas Room, Games Room |

## Setup (Raspberry Pi)

### 1. Install OS

Flash **Raspberry Pi OS Lite (32-bit, Bookworm)** using Raspberry Pi Imager.

### 2. Clone and install

```bash
git clone https://github.com/MArkFitz69/LearnGitHubCoPilot.git
cd LearnGitHubCoPilot
pip install -r requirements.txt
```

### 3. Configure credentials

```bash
# Set Hive credentials
export HIVE_USERNAME=your-email@example.com
export HIVE_PASSWORD=your-password
```

### 4. Discover Shelly Blu sensor

```bash
python -m zigbee_sensor_reader --discover-shelly
```

This will scan for 30 seconds and print the MAC address. Add it to `config.py`:

```python
SHELLY_SENSORS = {
    "AA:BB:CC:DD:EE:FF": "Attic",
}
```

### 5. Install systemd services

```bash
sudo cp zigbee_sensor_reader/zigbee-sensor-reader.service /etc/systemd/system/
sudo cp zigbee_sensor_reader/sensor-data-api.service /etc/systemd/system/

# Edit credentials/env vars in service files
sudo systemctl edit zigbee-sensor-reader
sudo systemctl edit sensor-data-api

# In sensor-data-api override, set:
# Environment=ONBOARDING_PASSCODE=your-strong-passcode
# Optional zigbee collector tuning (set in zigbee-sensor-reader override):
# Environment=ZIGBEE_ACTIVE_POLL_ON_STALE_CACHE=0
# Environment=ZIGBEE_STALE_AFTER_SECONDS=1800
# Environment=ZIGBEE_PERIODIC_LOG_INTERVAL_SECONDS=900
# Environment=ZIGBEE_HEARTBEAT_STALE_SECONDS=5400

# Enable and start
sudo systemctl enable --now zigbee-sensor-reader
sudo systemctl enable --now sensor-data-api
```

## Usage

### Collect data (runs continuously)

```bash
python -m zigbee_sensor_reader
```

### Test Hive connection

```bash
python -m zigbee_sensor_reader --hive
```

### Discover Shelly Blu sensors

```bash
python -m zigbee_sensor_reader --discover-shelly
```

### Start web API server

```bash
python -m zigbee_sensor_reader --serve --port 8080
```

### Pair new Zigbee sensors

```bash
python -m zigbee_sensor_reader --pair
```

Or use the web onboarding page: `http://<pi-ip>:8080/onboarding`

### Export data

```bash
python -m zigbee_sensor_reader --export csv
python -m zigbee_sensor_reader --export xlsx
python -m zigbee_sensor_reader --export csv --start 2026-01-01 --end 2026-03-31
```

## Power BI Integration

### Option 1: Web API (Recommended for live data)

With the API server running on the Pi, in Power BI Desktop:

1. **Get Data → Web**
2. Enter URL: `http://<pi-ip>:8080/api/readings?format=csv`
3. Set up scheduled refresh

Available endpoints:

| Endpoint | Description |
|----------|-------------|
| `/dashboard` | Live web dashboard (Sonoff, Hive, Shelly with daily stats) |
| `/onboarding` | Guided one-sensor onboarding page (passcode protected) |
| `/system` | Pi and application status page |
| `/api/status` | System overview (sensor count, latest reading) |
| `/api/dashboard` | Dashboard data as JSON (daily min/max + Hive runtime) |
| `/api/sensors` | All registered sensors with zones + freshness fields |
| `/api/readings?format=csv` | All readings as CSV |
| `/api/readings?zone=Zone 1&format=csv` | Filter by zone |
| `/api/readings?start=2026-01-01&end=2026-03-31&format=csv` | Filter by date |
| `/api/readings/latest?format=csv` | Latest reading per sensor + freshness fields |
| `/api/zigbee-heartbeat` | Per-sensor heartbeat summary (frame age/state) |
| `/api/export/csv` | Download full CSV file |

Onboarding-specific endpoints:
- `POST /api/onboarding/auth`
- `POST /api/onboarding/temp-passcode`
- `POST /api/onboarding/start-pairing`
- `POST /api/onboarding/save-sensor`
- `GET /api/onboarding/status`

## Web onboarding flow

1. Open `/onboarding` and unlock with `ONBOARDING_PASSCODE`.
2. (Optional) Generate a temporary 15-minute sharing passcode.
3. Start a 120-second pairing window.
4. Put one sensor into pairing mode.
5. Confirm candidate IEEE/model and first reading (up to 5 minutes).
6. Save friendly name + zone (writes to DB and `config.py`).

## Zigbee heartbeat and 15-minute logging behavior

- Sonoff battery sensors can stay at the same value for long periods.
- The collector now records:
  - frame heartbeat events in `zigbee_frame_events`
  - measurement rows with provenance fields in `readings`:
    - `reading_source` (`value_change`, `heartbeat_confirmed`, `active_poll_change`)
    - `source_event_age_seconds`
    - `is_stale`
- Unchanged values are still written at 15-minute intervals **only when heartbeat is healthy**.
- If heartbeat becomes stale, unchanged values are suppressed and `/system` flags the sensor as possible offline.

## Zigbee2MQTT mode (MQTT ingestion)

Set `ZIGBEE_BACKEND=z2m` to ingest Sonoff telemetry from Zigbee2MQTT MQTT topics instead of direct EZSP/TCP.

Recommended service environment:

```ini
Environment=ZIGBEE_BACKEND=z2m
Environment=Z2M_MQTT_HOST=home-logger.local
Environment=Z2M_MQTT_PORT=1883
Environment=Z2M_MQTT_TOPIC_PREFIX=zigbee2mqtt
# Optional:
# Environment=Z2M_MQTT_USERNAME=...
# Environment=Z2M_MQTT_PASSWORD=...
```

Notes:
- Hive and Shelly collectors remain unchanged.
- Sonoff 15-minute logging behavior remains unchanged.
- Keep `ZIGBEE_BACKEND=direct` available for immediate rollback.

## Syncing your existing names into Zigbee2MQTT

Preferred: rename in Zigbee2MQTT frontend after matching each device by IEEE.

MQTT bridge option (example):

```bash
mosquitto_pub -h home-logger.local -p 1883 \
  -t zigbee2mqtt/bridge/request/device/rename \
  -m '{"from":"0x00124b0025e7a1c3","to":"Living Room"}'
```

Repeat for each device so Zigbee2MQTT `friendly_name` matches the names already used in this project.

### Option 2: Export files

```bash
python -m zigbee_sensor_reader --export xlsx
```

Copy the Excel file to your PC and open in Power BI.

## Database Schema

```sql
-- readings table (one row per measurement)
SELECT timestamp, friendly_name, temperature_c, humidity_pct,
       zone, heating_on, boost_on, target_temp_c, heating_mode
FROM readings r JOIN sensors s ON r.ieee_address = s.ieee_address;
```

Key columns for heating analysis:
- `temperature_c` — Actual room temperature
- `target_temp_c` — Thermostat setpoint
- `heating_on` — 1 = boiler firing, 0 = off
- `boost_on` — 1 = boost override active
- `heating_mode` — OFF / SCHEDULE / MANUAL
- `zone` — Zone 1, Zone 2, or Zone 3

## Project Structure

```
zigbee_sensor_reader/
├── __init__.py
├── __main__.py              # CLI entry point
├── config.py                # Configuration (IPs, sensor names, zones)
├── database.py              # SQLite storage layer
├── zigbee_reader.py         # Zigbee coordinator (bellows/EZSP)
├── hive_reader.py           # Hive cloud API integration
├── shelly_ble_reader.py     # Shelly Blu H&T BLE scanner
├── web_server.py            # Flask API for remote data access
├── export.py                # CSV and Excel export
├── zigbee-sensor-reader.service  # systemd: data collector
└── sensor-data-api.service       # systemd: web API server
```
