# Sonoff Zigbee Sensor Reader

Python application to monitor home temperature and humidity for heating analysis. Collects data from multiple sources and stores it for Power BI visualization.

## Data Sources

| Source | Protocol | Data Collected |
|--------|----------|----------------|
| **Sonoff SNZB-02D/DR2** sensors | Zigbee via Dongle-M | Temperature, humidity, battery |
| **Hive smart plugs (via Zigbee2MQTT)** | Zigbee via Zigbee2MQTT | On/off state, power draw, energy, link quality |
| **Hive thermostats** | Cloud API | Temperature, target, heating on/off, boost, mode |
| **Shelly Blu H&T** | Bluetooth (BLE) | Temperature, humidity, battery |
| **ESP32 heating probes** | MQTT | Four Dallas probe temperatures |
| **Shelly Blu H&T forwarded by ESP32** | MQTT / BTHome v2 | Temperature, humidity, battery |

## Features

- 🌡️ Reads temperature, humidity, and battery from 10+ Zigbee sensors
- 🔌 Captures Hive plug state, live power draw, energy consumption, and link quality from Zigbee2MQTT
- 🔥 Captures Hive thermostat state: current temp, target, heating on/off, boost, mode
- 📡 Consolidates the Outdoor Shelly Blu H&T from direct BLE and ESP32 MQTT under one physical MAC identity
- 🌡️ Captures four ESP32-connected Dallas boiler probes via MQTT
- 📡 Decodes an ESP32-forwarded Shelly BTHome v2 payload and suppresses duplicate packet IDs
- 📈 Renders a dependency-free preceding-24-hour chart with 96 local 15-minute buckets and visible data gaps
- 🏠 Heating zone mapping, including Outdoor in Zone 4
- 🌐 Web API server for remote data access from Power BI
- ➕ Secure web onboarding flow for adding one Zigbee sensor at a time
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
| **ESP32 + Dallas probes** | Boiler flow/return temperatures published to MQTT |

## Collection architecture

The long-running collector starts independent MQTT tasks for Zigbee2MQTT and
the ESP32 feed, while the main loop continues polling Hive and direct Shelly
BLE. Both MQTT readers reconnect automatically and write to the same SQLite
schema through separate database connections.

The ESP32 reader subscribes only to its status topic and known sensor state
topics. Home Assistant discovery/config, debug, and other ESPHome topics are
ignored. The firmware typo `bolier_1_return` remains supported and is stored as
the canonical `Boiler 1 Return` sensor.

## Heating Zones

| Zone | Thermostat | Sensors |
|------|-----------|---------|
| **Zone 1** (Ground) | Hall | Living Room, Dining Room, Porch |
| **Zone 2** (First floor) | Master Bedroom | Guest Bedroom, Ensuite |
| **Zone 3** (Top floor) | Top Floor Landing | Blanca Room, Stellas Room, Games Room |
| **Zone 4** | — | Outdoor Shelly Blu H&T |

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

# Zigbee2MQTT broker (existing defaults shown)
export Z2M_MQTT_HOST=home-logger
export Z2M_MQTT_PORT=8081
export Z2M_MQTT_TRANSPORT=websockets
# export Z2M_MQTT_USER=your-user
# export Z2M_MQTT_PASS=your-password

# ESP32 node/topic prefix
export ESP32_TOPIC_PREFIX=heating-esp
```

The ESP32 reader inherits all `Z2M_MQTT_*` broker values by default. Set
`ESP32_MQTT_HOST`, `ESP32_MQTT_PORT`, `ESP32_MQTT_TRANSPORT`,
`ESP32_MQTT_USER`, or `ESP32_MQTT_PASS` only if the ESP32 publishes to a
different broker.

Optional default zones can be supplied without editing code:

```bash
export ESP32_ZONE_BOILER1_OUT="Heating"
export ESP32_ZONE_BOILER1_RETURN="Heating"
export ESP32_ZONE_BOILER2_OUT="Heating"
export ESP32_ZONE_BOILER2_RETURN="Heating"
export ESP32_ZONE_SHELLY="Zone 4"
```

Visible default names are `Boiler 1 Out`, `Boiler 1 Return`, `Boiler 2 Out`,
`Boiler 2 Return`, and `Outdoor`. Zones remain editable from the dashboard.

### 4. ESP32 MQTT topics

With the default prefix, the collector accepts:

```text
heating-esp/status
heating-esp/sensor/boiler1_out/state
heating-esp/sensor/bolier_1_return/state
heating-esp/sensor/boiler1_return/state
heating-esp/sensor/boiler2_out/state
heating-esp/sensor/boiler2_return/state
heating-esp/sensor/outdoorh_t_json/state
heating-esp/sensor/shelly_raw_payload/state
```

Dallas payloads are finite numeric Celsius values. The preferred Outdoor topic
contains the physical MAC and BTHome payload:

```json
{"mac":"94:B2:16:08:82:98","payload":"44007E01642E4345D900"}
```

The MAC is validated and normalized before the unencrypted BTHome v2 hex is
decoded. The older `shelly_raw_payload` compact or whitespace-separated hex
topic remains accepted during transition and uses the configured Outdoor MAC.
Malformed JSON, MACs, hex, encrypted/unsupported BTHome data, discovery, and
debug payloads are logged and ignored.

### 5. Discover Shelly Blu sensor

```bash
python -m zigbee_sensor_reader --discover-shelly
```

This will scan for 30 seconds and print the MAC address. Add it to `config.py`:

```python
SHELLY_SENSORS = {
    "94:B2:16:08:82:98": "Outdoor",
}
```

### 6. Install systemd services

```bash
sudo cp zigbee_sensor_reader/zigbee-sensor-reader.service /etc/systemd/system/
sudo cp zigbee_sensor_reader/sensor-data-api.service /etc/systemd/system/

# Edit credentials/env vars in service files
sudo systemctl edit zigbee-sensor-reader
sudo systemctl edit sensor-data-api

# In sensor-data-api override, set:
# Environment=ONBOARDING_PASSCODE=your-strong-passcode

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now zigbee-sensor-reader
sudo systemctl enable --now sensor-data-api
```

After deploying this update to an existing Pi, update the collector service
environment (at minimum `ESP32_TOPIC_PREFIX` if the default is not correct),
then reload and restart:

```bash
sudo systemctl daemon-reload
sudo systemctl restart zigbee-sensor-reader
sudo journalctl -u zigbee-sensor-reader -f
```

No separate ESP32 service or database migration command is required. The
collector creates the packet-deduplication state table automatically and
migrates the transitional `shelly:esp32:outdoor_ht` row into
`shelly:94:B2:16:08:82:98`, preserving readings while enforcing the configured
`Outdoor` / `Zone 4` presentation.

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
| `/dashboard` | Live dashboard with four current boiler probes, dedicated Outdoor metrics, and a local dependency-free 24-hour probe chart |
| `/onboarding` | Guided one-sensor onboarding page (passcode protected) |
| `/system` | Pi/application status, sensor health, and separate Zigbee, ESP32, Hive, and Shelly reading counts |
| `/api/status` | System overview (sensor count, latest reading) |
| `/api/system` | Full system status and source-specific database counts as JSON |
| `/api/dashboard` | Dashboard JSON including `esp32`, `outdoor`, and exact 96-bucket `probe_chart` data |
| `/api/sensors` | All registered sensors with zones |
| `/api/readings?format=csv` | All readings as CSV |
| `/api/readings?zone=Zone 1&format=csv` | Filter by zone |
| `/api/readings?start=2026-01-01&end=2026-03-31&format=csv` | Filter by date |
| `/api/readings/latest?format=csv` | Latest reading per sensor |
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
3. Start a 120-second Zigbee2MQTT pairing window.
4. Put one sensor into pairing mode so Zigbee2MQTT adds it first.
5. Confirm candidate IEEE/model and first reading after it syncs back here.
6. Save friendly name + zone into this logger (writes to DB and `config.py`).

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

ESP32 probe records use stable identities beginning with `esp32:`. The
forwarded Shelly uses the same physical identity as direct BLE,
`shelly:94:B2:16:08:82:98`, so both sources produce one Outdoor sensor and
share persistent packet-ID deduplication. Existing sensor, readings,
latest-reading, CSV, Excel, and Power BI surfaces include these records without
a schema fork.

The probe chart is visual-only and does not calculate boiler activity or
runtime. It covers exactly 96 local 15-minute buckets. Each bucket contains the
latest actual reading for each probe; missing buckets stay null so the HTML
chart renders a gap. Values are never averaged, smoothed, carried forward, or
interpolated.

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
├── esp32_mqtt_reader.py     # ESP32 Dallas + forwarded BTHome MQTT reader
├── web_server.py            # Flask API for remote data access
├── export.py                # CSV and Excel export
├── zigbee-sensor-reader.service  # systemd: data collector
└── sensor-data-api.service       # systemd: web API server
```
