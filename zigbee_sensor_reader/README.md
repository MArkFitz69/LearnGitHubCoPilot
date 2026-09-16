# Home Sensor Reader

Python service for collecting home heating and environmental telemetry into
SQLite and exposing read-only dashboards, CSV, Excel, and Power BI feeds.

## Data sources

| Source | Integration | Measurements |
|---|---|---|
| Sonoff and Hive Zigbee devices | Zigbee2MQTT over MQTT | Temperature, humidity, battery, state, power, energy |
| ESP32 heating node | MQTT | Four Dallas boiler flow/return temperatures |
| Outdoor Shelly Blu H&T | ESP32-forwarded BTHome v2, with direct BLE fallback | Temperature, humidity, battery |
| Attic Shelly Blu H&T | Direct BLE and Zigbee2MQTT | Temperature, humidity, battery |
| Hive thermostats/hot water | Hive cloud API | Current/target temperature, mode, heating and boost state |

Zigbee2MQTT is the only Zigbee integration. Pair, rename, and describe Zigbee
devices in Zigbee2MQTT; the collector consumes its MQTT metadata and readings.

Outdoor is stored as `shelly:94:B2:16:08:82:98` (`Outdoor`, Zone 4). Attic is
stored as `shelly:FC:4D:6A:1D:1D:FB` (`Attic`, Zone 5). The Attic Zigbee2MQTT
EUI-64 `fc:4d:6a:ff:fe:1d:1d:fb` is migrated and canonicalized automatically.
Historical readings and packet state are preserved.

## Architecture and behavior

The collector runs independent Zigbee2MQTT and ESP32 MQTT clients while the
async main loop polls Hive and direct Shelly BLE. MQTT callbacks use their own
SQLite connections; writes use WAL, a busy timeout, and atomic transactions.
Schema/data migrations are versioned with `PRAGMA user_version`.

The ESP32 reader subscribes only to:

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

The misspelled `bolier_1_return` topic remains compatible. The preferred
Outdoor payload is:

```json
{"mac":"94:B2:16:08:82:98","payload":"44007E01642E4345D900"}
```

Compact or whitespace-separated plain BTHome v2 hex is accepted; malformed,
unsupported, or encrypted payloads are logged and ignored. Packet IDs are
deduplicated persistently with 8-bit sequence handling. Outdoor prefers ESP32
and falls back to BLE only after `SHELLY_SOURCE_FALLBACK_SECONDS` (default 1800)
without an ESP32 packet. Attic prefers BLE. When an authoritative source keeps
republishing the same valid BTHome advertisement, rapid repeats are rejected,
but one unchanged heartbeat is stored after
`SHELLY_HEARTBEAT_REPEAT_SECONDS` (default 300). This keeps sensor timestamps
current without recording every MQTT retransmission. Accepted and rejected
packet decisions are logged at INFO with identity, source, packet ID, delta,
and reason.

## Raspberry Pi setup

```bash
git clone https://github.com/MArkFitz69/LearnGitHubCoPilot.git
cd LearnGitHubCoPilot
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Core environment:

```bash
export HIVE_USERNAME=your-email@example.com
export HIVE_PASSWORD=your-hive-secret
export Z2M_MQTT_HOST=home-logger
export Z2M_MQTT_PORT=8081
export Z2M_MQTT_TRANSPORT=websockets
export ESP32_TOPIC_PREFIX=heating-esp
export SHELLY_HEARTBEAT_REPEAT_SECONDS=300
```

The ESP32 client inherits every Z2M broker setting unless the corresponding
`ESP32_MQTT_*` variable is set.

The existing ESP32 subscription also records device-level MQTT health. A
non-retained recognized sensor publication or non-retained `online` status
confirms live communication even when the sensor payload is malformed; malformed
payloads still never create readings. Retained `online` replays do not refresh
liveness, while explicit `offline` status is immediate. Health becomes stale
after 30 minutes by default:

```bash
export ESP32_MQTT_STALE_MINUTES=30
```

### MQTT production hardening

Use a dedicated Mosquitto account restricted by ACL to read
`zigbee2mqtt/#` and the exact `heating-esp` topics. Do not grant publish access
except where the Zigbee2MQTT device-list request requires it. Configure:

```bash
export Z2M_MQTT_USER=sensor-reader
export Z2M_MQTT_PASS=your-service-secret
export Z2M_MQTT_TLS=true
export Z2M_MQTT_CA_CERT=/etc/ssl/certs/home-mqtt-ca.pem
```

Optional mutual TLS uses `Z2M_MQTT_CLIENT_CERT` and
`Z2M_MQTT_CLIENT_KEY`. TLS always validates the broker certificate and
hostname; there is no insecure verification mode. These settings work with
TCP/TLS or WebSockets/WSS according to `Z2M_MQTT_TRANSPORT`.

Local unencrypted MQTT remains supported for an existing trusted single-host
setup, but credentials, ACLs, and TLS are recommended whenever traffic crosses
the LAN.

### Services

```bash
sudo cp zigbee_sensor_reader/zigbee-sensor-reader.service /etc/systemd/system/
sudo cp zigbee_sensor_reader/sensor-data-api.service /etc/systemd/system/
sudo systemctl edit zigbee-sensor-reader
sudo systemctl edit sensor-data-api
sudo systemctl daemon-reload
sudo systemctl enable --now zigbee-sensor-reader sensor-data-api
```

Set `WEB_HOST` to the Pi's private LAN address where practical. The web service
is intentionally passwordless and read-only for a trusted home LAN. Restrict
TCP port 8080 to the home subnet with the host/router firewall and **never
port-forward or expose it to the internet**. Example with UFW:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
sudo ufw deny 8080/tcp
```

## Commands

```bash
python -m zigbee_sensor_reader
python -m zigbee_sensor_reader --hive
python -m zigbee_sensor_reader --discover-shelly
python -m zigbee_sensor_reader --serve --port 8080
python -m zigbee_sensor_reader --summary
python -m zigbee_sensor_reader --export csv
python -m zigbee_sensor_reader --export xlsx
```

## Read-only web surfaces

| Endpoint | Description |
|---|---|
| `/dashboard` | Boiler probes, exact 96-bucket local 24-hour chart, Hive, Outdoor, Attic, and plugs |
| `/system` | Pi/service/database status and sensor health |
| `/api/status` | Lightweight status |
| `/api/system` | Full system status and source counts |
| `/api/dashboard` | Dashboard JSON and chart series |
| `/api/sensors` | Sensor registry with effective zones |
| `/api/readings` | Filterable readings; add `format=csv` for Power BI |
| `/api/readings/latest` | Exactly one latest row per sensor |
| `/api/export/csv` | Download all matching readings |

`/api/system` includes structured `esp32_mqtt_health` entries. `/system`
renders the same state, last confirmed communication, age, last MQTT status,
retained-status indicator, and last live topic after the Services table.

Zones are configured in `config.py` or derived from Zigbee2MQTT descriptions;
the website does not mutate configuration or database metadata. All APIs,
dashboard queries, CSV exports, and Power BI feeds expose the same effective
zone precedence: Zigbee2MQTT description, configured sensor zone, then the
historical reading zone. Configured canonical Outdoor/Attic zones remain
authoritative. Zigbee2MQTT descriptions are cached across ordinary state
messages that omit device metadata; clearing a description in Zigbee2MQTT
clears the derived zone on the next bridge device-list update.

The boiler chart uses exactly 96 local 15-minute buckets. Each sensor/bucket
contains only the latest actual reading; gaps remain null. Values are never
averaged, smoothed, carried forward, jittered, or converted into runtime.

## Project structure

```text
zigbee_sensor_reader/
├── __main__.py
├── config.py
├── database.py
├── mqtt_config.py
├── z2m_reader.py
├── esp32_mqtt_reader.py
├── shelly_ble_reader.py
├── hive_reader.py
├── web_server.py
├── export.py
├── zigbee-sensor-reader.service
└── sensor-data-api.service
```
